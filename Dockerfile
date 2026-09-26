FROM python:3.12-slim
WORKDIR /app
COPY bot.py composer.py reply_engine.py polish.py ./
# stdlib only — no pip install needed
EXPOSE 8080
CMD ["python", "bot.py"]
