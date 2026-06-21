FROM nginx:alpine

# Shell chrome is a single-page app served as static files.
COPY nginx.conf      /etc/nginx/conf.d/default.conf
COPY index.html      /usr/share/nginx/html/index.html
COPY shell.js        /usr/share/nginx/html/shell.js
COPY style.css       /usr/share/nginx/html/style.css
COPY modules         /usr/share/nginx/html/modules

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
