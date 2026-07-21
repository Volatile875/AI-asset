FROM python:3.11-slim
WORKDIR /app
# Corporate TLS-inspection root CA (Cloudflare Gateway) so pip + runtime HTTPS verify
COPY corp-ca.crt /usr/local/share/ca-certificates/corp-ca.crt
RUN cat /usr/local/share/ca-certificates/corp-ca.crt >> /etc/ssl/certs/ca-certificates.crt
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Also trust the corporate CA in certifi's bundle (used by pinecone/openai/httpx/requests)
RUN cat /usr/local/share/ca-certificates/corp-ca.crt >> "$(python -c 'import certifi; print(certifi.where())')"
COPY . .
EXPOSE 8003
CMD ["uvicorn", "app.graph_services_main:app", "--host", "0.0.0.0", "--port", "8003", "--reload"]
