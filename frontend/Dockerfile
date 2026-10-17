# Development Dockerfile for Shizuha Connect Frontend
FROM node:20-alpine

WORKDIR /app

# Create packages directory (will be volume-mounted in dev)
RUN mkdir -p /packages

# Install dependencies
COPY package*.json ./
COPY vendor/ ./vendor/
RUN npm ci --ignore-scripts --no-audit --no-fund

COPY . .

EXPOSE 80
CMD ["npm", "run", "dev"]
