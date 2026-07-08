from flask import Flask, request, jsonify
import logging

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,  # ERROR 레벨 이상만 보여줌
    format='%(asctime)s - %(levelname)s - %(message)s'  # 시간, 레벨, 메시지 형식
)

@app.route('/')
def hello():
    return "Hello, World!"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
