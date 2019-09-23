import json
import os
import slack
from typing import Tuple
from slackeventsapi import SlackEventAdapter
from kubernetes import client, config
from kubernetes.client.rest import ApiException

ITEM_PREFIX = '\n    '

POD = 'pod'
DEPLOYMENT = 'deployment'
HPA = 'hpa'

SLACK_SIGNING_SECRET = os.environ['SLACK_SIGNING_SECRET']
SLACK_OAUTH_ACCESS_TOKEN = os.environ['SLACK_OAUTH_ACCESS_TOKEN']
SLACK_ALLOWED_CHANNEL = os.getenv('SLACK_ALLOWED_CHANNEL', '')
TARGET_NAMESPACE = os.getenv('K8S_TARGET_NAMESPACE', 'default')


slack_events_adapter = SlackEventAdapter(SLACK_SIGNING_SECRET, endpoint='/slack/events')
slack_client = slack.WebClient(token=SLACK_OAUTH_ACCESS_TOKEN)
config.incluster_config.load_incluster_config()
core_v1 = client.CoreV1Api()
app_v1 = client.AppsV1Api()
autoscaling_v1 = client.AutoscalingV1Api()
channel_name_cache = dict()
user_name_cache = dict()
k8s_get_resource_method = dict()
k8s_get_all_resource_method = dict()


def delete_pod(pods: list) -> str:
    result = list()
    for pod in pods:
        try:
            print(f'deleteing `{pod}`')
            core_v1.delete_namespaced_pod(namespace=TARGET_NAMESPACE, name=pod)
            result.append(f'`{pod}` deleted')
        except ApiException:
            result.append(f"Can't delete `{pod}`")
    return 'Delete pod(s):{}'.format(ITEM_PREFIX+ITEM_PREFIX.join(result))


def delete_handler(request: list) -> str:
    if request[0] == 'pod':
        if len(request) < 2:
            return 'Missed target pod to delete.'
        return delete_pod(request[1:])
    else:
        return 'you only can delete pod(s).'
    
    
def get_k8s_resource(kind: str, resources: list) -> Tuple[list, list]:
    errors = list()
    if resources is None:
        result = k8s_get_all_resource_method[kind](namespace=TARGET_NAMESPACE).items
    else:
        result = list()
        for resource in resources:
            try:
                result.append(k8s_get_resource_method[kind](namespace=TARGET_NAMESPACE, name=resource))
            except ApiException:
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
        
    return 'Deployment status:{}'.format(ITEM_PREFIX+ITEM_PREFIX.join(result))


def get_hpa(needed_hpa: list) -> str:
    hpas, result = get_k8s_resource(HPA, needed_hpa)
                
    for hpa in hpas:
        name = hpa.metadata.name
        min_replicas = hpa.spec.min_replicas
        max_replicas = hpa.spec.max_replicas
        
        # target_metrics = hpa.metadata.annotations['autoscaling.alpha.kubernetes.io/metrics']
        # current_metrics = hpa.metadata.annotations['autoscaling.alpha.kubernetes.io/current-metrics']
        
        result.append(f'`{name}`: Min:{min_replicas} Max:{max_replicas}')
        
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
                # TODO: Log it
                return False
        else:
            # Only allow private channel. Because public everyone can join. No meaning for checking
            channel_info = slack_client.groups_info(channel=channel_id)
            if channel_info['ok']:
                channel_name_cache[channel_id] = channel_info['group']['name']
                if channel_info['group']['name'] != SLACK_ALLOWED_CHANNEL:
                    # If configed channel name and received channel name not match. Ignore request
                    # TODO: Log it
                    return False
            else:
                # Get private channel info error, or not private channel. Ignore request. Not cache it.
                # Because if just insufficient permission. User can add permission and re-install slack app anytime.
                # If cached, user need restart this app too. (Or need add expire for cache?)
                # TODO: Log it
                return False
    return True


# Create an event listener for "reaction_added" events and print the emoji name
@slack_events_adapter.on("app_mention")
def app_mention(event_data: dict):
    sender_id = event_data['event']['user']
    channel_id = event_data['event']['channel']
    
    if not request_in_right_channel(channel_id):
        return

    if len(event_data['authed_users']) != 1:
        slack_client.chat_postMessage(channel=channel_id, text=f"<@{sender_id}>, Only can mention this bot. No others.")
        return
    my_id = event_data['authed_users'][0]
    my_id_in_slack_format = f'<@{my_id}>'
    if not event_data['event']['text'].startswith(my_id_in_slack_format):
        slack_client.chat_postMessage(channel=channel_id, text=f"<@{sender_id}>, Must mention this bot at begin.")
        return
    
    if sender_id not in user_name_cache:
        sender_info = slack_client.users_info(user=sender_id)
        if sender_info['ok']:
            user_name_cache[sender_id] = sender_info['user']['name']
        
    request_string = event_data['event']['text'][len(my_id_in_slack_format)+1:]
    
    # TODO: Log request with sender_name and sender_id
    
    response = request_handler(request_string)
    
    # print(event_data)
    slack_client.chat_postMessage(channel=channel_id, text=f'<@{sender_id}>, {response}')
    
    
def setup_k8s_method():
    k8s_get_resource_method[POD] = core_v1.read_namespaced_pod
    k8s_get_all_resource_method[POD] = core_v1.list_namespaced_pod
    
    k8s_get_resource_method[DEPLOYMENT] = app_v1.read_namespaced_deployment
    k8s_get_all_resource_method[DEPLOYMENT] = app_v1.list_namespaced_deployment
    
    k8s_get_resource_method[HPA] = autoscaling_v1.read_namespaced_horizontal_pod_autoscaler
    k8s_get_all_resource_method[HPA] = autoscaling_v1.list_namespaced_horizontal_pod_autoscaler


def main():
    setup_k8s_method()
    
    # Start the server on port 8080
    slack_events_adapter.start(host='0.0.0.0', port=8080)
    

if __name__ == '__main__':
    main()
