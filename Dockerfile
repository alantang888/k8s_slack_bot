FROM python:3.9-alpine3.14

COPY . /app
WORKDIR /app
RUN apk add git && pip install pipenv && pipenv install --system --deploy

CMD ["python", "/app/k8s_slack_bot.py"]
