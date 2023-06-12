FROM python:3.10-alpine

RUN mkdir /app
WORKDIR /app
COPY requirements.txt /app/
RUN pip install -r requirements.txt

COPY syncthing_ignore.py /app/
ENTRYPOINT [ "/app/syncthing_ignore.py" ]
