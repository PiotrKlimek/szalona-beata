FROM python:3.12-slim
WORKDIR /app
COPY summarize.py sample_data.py test_summarize.py ./
RUN python -m unittest -q
ENTRYPOINT ["python", "-I", "/app/summarize.py"]
