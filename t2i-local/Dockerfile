FROM mcr.microsoft.com/playwright:latest-python

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 6199

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "6199"]
