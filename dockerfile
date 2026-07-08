ARG imagepath

FROM ${imagepath}/back-baseimage:latest

COPY . /app
WORKDIR /app

#EXPOSE 8080

## root 유저 사용을 막기 위함
USER appuser

CMD ["python3", "search.py"]
