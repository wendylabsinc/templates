# Tiny, multi-arch (arm64 ✓ for Jetson) static file server for the hackathon page.
FROM nginx:alpine

# Serve on port 80 (host networking → clean URL: http://wendyos-jetson-2.local).
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html qr.html onboarding.html feedback.html admin.html /usr/share/nginx/html/

EXPOSE 80
