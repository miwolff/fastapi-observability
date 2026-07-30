FROM python:3.12

RUN pip install uv

WORKDIR /app
COPY ./ ./
RUN uv pip install --no-cache --system -r requirements.txt

RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app && chown -R app:app /app
USER app

EXPOSE 8000

CMD ["python", "main.py"]
