    FROM public.ecr.aws/docker/library/python:3.12-slim

    WORKDIR /app

    COPY . .

    RUN pip install -r requirements.txt


    EXPOSE 8000

    CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "2", "-b", "0.0.0.0:8000", "app:app"]