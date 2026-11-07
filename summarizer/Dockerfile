FROM python:3.12-slim
WORKDIR /app
# Librus grades (optional section of the digest); everything else is stdlib
RUN pip install --no-cache-dir librus-synergia==0.3.30
COPY summarize.py librus_grades.py sample_data.py test_summarize.py test_librus_grades.py ./
RUN python -m unittest -q
ENTRYPOINT ["python", "-I", "/app/summarize.py"]
