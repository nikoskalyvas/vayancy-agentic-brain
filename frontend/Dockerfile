FROM node:22-alpine AS base

WORKDIR /app
COPY package.json ./
RUN npm install --frozen-lockfile

COPY . .

ARG NEXT_PUBLIC_API_URL=http://api:8000
ARG NEXT_PUBLIC_PROPERTY_ID=default
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_PROPERTY_ID=$NEXT_PUBLIC_PROPERTY_ID

RUN npm run build

CMD ["npm", "start"]
