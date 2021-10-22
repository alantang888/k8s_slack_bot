import json
import logging
import os
import sys
import slack_sdk
from typing import Tuple
from stackdriver_json_formatter import StackdriverJsonFormatter
from slackeventsapi import SlackEventAdapter
from kubernetes import client, config
from kubernetes.client.rest import ApiException

ITEM_PREFIX = '\n    '

POD = 'pod'
NODE = 'node'
DEPLOYMENT = 'deployment'
HPA = 'hpa'
KIND_DAEMONSET = 'DaemonSet'

SLACK_SIGNING_SECRET = os.environ['SLACK_SIGNING_SECRET']
SLACK_OAUTH_ACCESS_TOKEN = os.environ['SLACK_OAUTH_ACCESS_TOKEN']
SLACK_ALLOWED_CHANNEL = os.getenv('SLACK_ALLOWED_CHANNEL', '')
TARGET_NAMESPACE = os.getenv('K8S_TARGET_NAMESPACE', 'default')


slack_events_adapter = SlackEventAdapter(SLACK_SIGNING_SECRET, endpoint='/slack/events')
slack_client = slack_sdk.WebClient(token=SLACK_OAUTH_ACCESS_TOKEN)
config.incluster_config.load_incluster_config()
core_v1 = client.CoreV1Api()
app_v1 = client.AppsV1Api()
autoscaling_v1 = client.AutoscalingV1Api()
channel_name_cache = dict()
user_name_cache = dict()
k8s_get_resource_method = dict()
k8s_get_all_resource_method = dict()
log = None


def delete_pod(pods: list) -> str:
    result = list()
    for pod in pods:
        try:
            log.info(f'deleteing `{pod}`')
            core_v1.delete_namespaced_pod(namespace=TARGET_NAMESPACE, name=pod)
            result.append(f'`{pod}` deleted')
        except ApiException as e:
            log.error(f'Delete pod "{pod}" from k8s error', {'error': e.body})
            result.append(f"Can't delete `{pod}`")
    return 'Delete pod(s):{}'.format(ITEM_PREFIX+ITEM_PREFIX.join(result))


def is_daemonset_pod(pod: client.models.v1_pod.V1Pod) -> bool:
    if pod.metadata.owner_references is None or pod.metadata.owner_references[0].kind == KIND_DAEMONSET:
        return True
    return False


def drain_node(node: str):
    core_v1.patch_node(node, body={"spec":{"unschedulable":True}})
    pods = core_v1.list_pod_for_all_namespaces(field_selector=f'spec.nodeName={node}').items
    for pod in pods:
        if is_daemonset_pod(pod):
            continue
        pod_meta = client.V1ObjectMeta(name=pod.metadata.name, namespace=pod.metadata.namespace)
        eviction = client.V1beta1Eviction(metadata=pod_meta,
                                          delete_options=client.V1DeleteOptions(grace_period_seconds=60))
        core_v1.create_namespaced_pod_eviction(name=pod.metadata.name, namespace=pod.metadata.namespace, body=eviction)


def delete_node(nodes: list) -> str:
    result = list()
    for node in nodes:
        try:
            drain_node(node)
            result.append(f'{node} drained. Let autoscaler remove it later')
        except Exception as e:
            log.error(f'Drain node "{node}" from k8s error', {'error': e.body})
            result.append(f'{node} drain error')
    return f'Drained node(s):{ITEM_PREFIX+ITEM_PREFIX.join(result)}'


def delete_handler(request: list) -> str:
    if request[0] == POD:
        if len(request) < 2:
            return 'Missed target pod to delete.'
        return delete_pod(request[1:])
    elif request[0] == NODE:
        if len(request) < 2:
            return 'Missed target node to delete.'
        return delete_node(request[1:])
    else:
        return 'you only can delete pod(s) or node(s).'
    
    
def get_k8s_resource(kind: str, resources: list) -> Tuple[list, list]:
    errors = list()
    if resources is None:
        result = k8s_get_all_resource_method[kind](namespace=TARGET_NAMESPACE).items
    else:
        result = list()
        for resource in resources:
            try:
                result.append(k8s_get_resource_method[kind](namespace=TARGET_NAMESPACE, name=resource))
            except ApiException as e:
                log.error(f'Get {kind} from k8s error', {'kind': kind, 'resources': resources, 'error': e.body})
                errors.append(f'`{resource}`: Not found')
    return result, errors


def get_pod(needed_pods: list) -> str:
    pods, result = get_k8s_resource(POD, needed_pods)
        
    for pod in pods:
        name = pod.metadata.name
        status = pod.status.phase
        result.append(f'`{name}`: {status}')
        
    return 'Pod status:{}'.format(ITEM_PREFIX+ITEM_PREFIX.join(result))


def get_deployment(needed_deployments: list) -> str:
    deployments, result = get_k8s_resource(DEPLOYMENT, needed_deployments)
            
    for deployment in deployments:
        name = deployment.metadata.name
        num_replica = deployment.status.ready_replicas
        image_tag = deployment.spec.template.spec.containers[0].image.split(':')[-1]
        result.append(f'`{name}`: ready replicas: {num_replica}, image:{image_tag}')
        
    return 'Deployment status (Just showing latest deployment config, doesn\'t means your update is success. Detail can use argo cd):{}'.format(ITEM_PREFIX+ITEM_PREFIX.join(result))


def get_hpa_target_type(hpa_target: dict) -> str:
    for key in hpa_target.keys():
        if key.startswith('target'):
            return key[len('target'):]
    log.debug("HPA metric don't have key started with target", {'hpa_resource': hpa_target})
    return ''


def get_hpa(needed_hpa: list) -> str:
    hpas, result = get_k8s_resource(HPA, needed_hpa)
                
    for hpa in hpas:
        name = hpa.metadata.name
        min_replicas = hpa.spec.min_replicas
        max_replicas = hpa.spec.max_replicas
        
        result.append(f'`{name}`: Min:{min_replicas} Max:{max_replicas}')
        
        if (
                'autoscaling.alpha.kubernetes.io/metrics' not in hpa.metadata.annotations or
                'autoscaling.alpha.kubernetes.io/current-metrics' not in hpa.metadata.annotations
           ):
            continue
            
        target_metrics = json.loads(hpa.metadata.annotations['autoscaling.alpha.kubernetes.io/metrics'])
        current_metrics = json.loads(hpa.metadata.annotations['autoscaling.alpha.kubernetes.io/current-metrics'])
        
        if needed_hpa is None:
            # Not show metric detail when get all HPAs
            continue
        for metric in target_metrics:
            if metric['type'] == 'Resource':
                metric_name = metric['resource']['name']
                target_metric_type = get_hpa_target_type(metric['resource'])
                target_value = metric['resource'][f'target{target_metric_type}']
                current_value = [current_metric['resource'][f'current{target_metric_type}']
                                 for current_metric in current_metrics if current_metric['type'] == 'Resource'
                                 and current_metric['resource']['name'] == metric_name][0]
            elif metric['type'] == 'External':
                metric_name = metric['external']['metricName']
                target_metric_type = get_hpa_target_type(metric['external'])
                target_value = metric['external'][f'target{target_metric_type}']
                current_value = [current_metric['external'][f'current{target_metric_type}']
                                 for current_metric in current_metrics if current_metric['type'] == 'External'
                                 and current_metric['external']['metricName'] == metric_name][0]
            else:
                # Don't know this type. Log it?
                continue
            result.append(f'    {metric_name}: {current_value}/{target_value}')
        
    return 'HPA config:{}'.format(ITEM_PREFIX+ITEM_PREFIX.join(result))


def get_handler(request: list) -> str:
    if request[0] == POD:
        return get_pod(request[1:] if len(request) > 1 else None)
    elif request[0] == DEPLOYMENT:
        return get_deployment(request[1:] if len(request) > 1 else None)
    elif request[0] == HPA:
        return get_hpa(request[1:] if len(request) > 1 else None)
    else:
        return 'Unknown resource to get.'


def request_handler(request: str) -> str:
    requests_list = request.split()
    if len(requests_list) < 2:
        return 'Error'
    
    if requests_list[0] == 'delete':
        return delete_handler(requests_list[1:])
    elif requests_list[0] == 'get':
        return get_handler(requests_list[1:])
    else:
        return 'Unknown action.'
    
    
def request_in_right_channel(channel_id: str) -> bool:
    # Check channel name meet config. If not set will ignore checking
    if SLACK_ALLOWED_CHANNEL != '':
        if channel_id in channel_name_cache:
            if channel_name_cache[channel_id] != SLACK_ALLOWED_CHANNEL:
                log.info(f'Received request not in {channel_name_cache[channel_id]}. Which is not allowed')
                return False
        else:
            # Only allow private channel. Because public everyone can join. No meaning for checking
            channel_info = slack_client.conversations_info(channel=channel_id)
            if channel_info['ok']:
                channel_name_cache[channel_id] = channel_info['channel']['name']
                if channel_info['channel']['name'] != SLACK_ALLOWED_CHANNEL:
                    # If configed channel name and received channel name not match. Ignore request
                    log.info(f'Received request not in {channel_name_cache[channel_id]}. Which is not allowed')
                    return False
            else:
                # Get private channel info error, or not private channel. Ignore request. Not cache it.
                # Because if just insufficient permission. User can add permission and re-install slack app anytime.
                # If cached, user need restart this app too. (Or need add expire for cache?)
                log.info(f"Can't lookup channel ID {channel_id} by groups.info. It may not exist or not private channel")
                return False
    return True


# Create an event listener for "reaction_added" events and print the emoji name
@slack_events_adapter.on("app_mention")
def app_mention(event_data: dict):
    sender_id = event_data['event']['user']
    channel_id = event_data['event']['channel']
    
    if not request_in_right_channel(channel_id):
        return

    if len(event_data['authorizations']) != 1:
        slack_client.chat_postMessage(channel=channel_id, text=f"<@{sender_id}>, Only can mention this bot. No others.")
        return
    my_id = event_data['authorizations'][0]['user_id']
    my_id_in_slack_format = f'<@{my_id}>'
    if not event_data['event']['text'].startswith(my_id_in_slack_format):
        slack_client.chat_postMessage(channel=channel_id, text=f"<@{sender_id}>, Must mention this bot at begin.")
        return
    
    if sender_id not in user_name_cache:
        sender_info = slack_client.users_info(user=sender_id)
        if sender_info['ok']:
            user_name_cache[sender_id] = sender_info['user']['name']
        
    request_string = event_data['event']['text'][len(my_id_in_slack_format)+1:]
    
    log.info('Request received.',
                 {
                     'sender_name': user_name_cache[sender_id] if sender_id in user_name_cache else 'LOOKUP_ERROR',
                     'sender_id': sender_id, 'channel_id': channel_id, 'request': request_string,
                     'event_id': event_data['event_id']
                  })
    
    response = request_handler(request_string)
    
    # print(event_data)
    slack_client.chat_postMessage(channel=channel_id, text=f'<@{sender_id}>, {response}')
    log.info('Request handled.', {'event_id': event_data['event_id']})
    
    
def setup_k8s_method():
    k8s_get_resource_method[POD] = core_v1.read_namespaced_pod
    k8s_get_all_resource_method[POD] = core_v1.list_namespaced_pod
    
    k8s_get_resource_method[DEPLOYMENT] = app_v1.read_namespaced_deployment
    k8s_get_all_resource_method[DEPLOYMENT] = app_v1.list_namespaced_deployment
    
    k8s_get_resource_method[HPA] = autoscaling_v1.read_namespaced_horizontal_pod_autoscaler
    k8s_get_all_resource_method[HPA] = autoscaling_v1.list_namespaced_horizontal_pod_autoscaler
    
    
def setup_logger():
    global log
    log = logging.getLogger('slack_bot')
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(StackdriverJsonFormatter())
    log.addHandler(stream)
    log.setLevel(os.getenv('LOG_LEVEL', 'INFO'))


def main():
    setup_logger()
    setup_k8s_method()
    
    # Start the server on port 8080
    slack_events_adapter.start(host='0.0.0.0', port=8080)
    

if __name__ == '__main__':
    main()
