FROM nginx:alpine

# Serve the single-page app + any static assets
COPY ./index.html  /usr/share/nginx/html/index.html
COPY ./static      /usr/share/nginx/html/static

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
