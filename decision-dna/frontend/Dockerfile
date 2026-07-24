FROM node:20-alpine
WORKDIR /app

# Trust the corporate TLS root CA for secure npm installations
COPY corp-ca.crt /usr/local/share/ca-certificates/corp-ca.crt
RUN cat /usr/local/share/ca-certificates/corp-ca.crt >> /etc/ssl/certs/ca-certificates.crt
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt
RUN npm config set strict-ssl false

COPY package.json package-lock.json* ./
RUN npm install

COPY . .
RUN npm run build
RUN npm install -g serve

EXPOSE 8501
CMD ["serve", "-s", "dist", "-l", "8501"]
