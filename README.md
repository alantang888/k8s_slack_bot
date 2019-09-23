k8s_slack_bot

# What is this app?
This is a slack app help to perform simple operations from Slack (users no need to have K8S access/permission to K8S)

Like:
- Get/Delete pods
- Get deployments ready count and image tag
- Get HPA min/max replicas

This is for let some people don't know about K8S. But need to perform some simple action for K8S operations.
No any permission check. 
(Will add check on call from which slack channel. Then only need to control who is on that slack channel for operations)

I thinking is it use Slack commands for this app. Now is only response to @mention

# Config
Use environment variables for configuration:
- `SLACK_SIGNING_SECRET`: Slack Signing Secret (On your slack app basic information page)
- `SLACK_OAUTH_ACCESS_TOKEN`: Slack OAuth Access Token (On your slack app install app page)
- `SLACK_ALLOWED_CHANNEL`: Which Slack channel allowed send command to this app (Not implement yet)
- `K8S_TARGET_NAMESPACE`: Which K8S namespace for operations 

# Required Slack Scopes
- channels:read
- chat:write:bot
- bot
- users:read
