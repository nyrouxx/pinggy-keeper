FROM node:24-alpine

WORKDIR /app
COPY keeper.mjs ./
USER node
CMD ["node", "keeper.mjs"]
