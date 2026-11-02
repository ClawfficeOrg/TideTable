# TideTable dashboard image (task 1.3.1 scaffold).
# Compose builds ./web and routes tk.example.com to port 3000.
FROM node:24.22.1-alpine AS build
# Trust store hardening: the eMudhra intermediate CA private keys were
# compromised (2026-10-20) and could be recovered from the public
# certificates, so any client still trusting these roots can be impersonated:
# "emSign ECC Root CA - G3" and "AAA Certificate Services".
#
# On Alpine the trust store is split across two packages and BOTH must be
# upgraded: `ca-certificates` holds the individual .pem files (and regenerates
# the bundle from them) while `ca-certificates-bundle` holds the aggregate.
# Upgrading only the bundle leaves the affected root in place.
# See https://emudhra.we-have-a-problem.com
RUN apk add --no-cache --upgrade ca-certificates ca-certificates-bundle
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

FROM node:24.22.1-alpine
# Trust store hardening: the eMudhra intermediate CA private keys were
# compromised (2026-10-20) and could be recovered from the public
# certificates, so any client still trusting these roots can be impersonated:
# "emSign ECC Root CA - G3" and "AAA Certificate Services".
#
# On Alpine the trust store is split across two packages and BOTH must be
# upgraded: `ca-certificates` holds the individual .pem files (and regenerates
# the bundle from them) while `ca-certificates-bundle` holds the aggregate.
# Upgrading only the bundle leaves the affected root in place.
# See https://emudhra.we-have-a-problem.com
RUN apk add --no-cache --upgrade ca-certificates ca-certificates-bundle
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/build ./build
COPY --from=build /app/package.json ./
RUN npm install --omit=dev
EXPOSE 3000
ENV PORT=3000
CMD ["node", "build"]
