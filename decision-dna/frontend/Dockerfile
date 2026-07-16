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
EXPOSE 8501
# Serve on 8501 (Streamlit's native port), NOT 3000. Streamlit's client bundle has a
# hardcoded dev-mode heuristic -- `3e3===+window.location.port` -- so when the page is
# served on port 3000 it assumes the backend is on 8501 and shows "Connection error".
# --browser.serverPort does NOT override this, so the only reliable fix is to avoid 3000.
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true", "--browser.gatherUsageStats=false"]
