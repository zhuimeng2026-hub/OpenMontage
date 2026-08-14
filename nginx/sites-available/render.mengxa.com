# render.mengxa.com — FrameFlow BFF + 前端 SPA（HTTPS）

# HTTP → HTTPS redirect（微信网页授权域名校验文件例外，见下）
server {
    listen 80;
    listen [::]:80;
    server_name render.mengxa.com;

    root /opt/OpenMontage/frameflow/bff/web;

    # 微信「网页授权域名」校验：后台按 http 直接下载 MP_verify_*.txt，
    # 若 301 跳 https 会导致校验失败、域名无法保存，扫码即报 redirect_uri 不一致。
    # 故该文件在 http 下直接 200，其余路径才跳 https。
    location ~ ^/MP_verify_[A-Za-z0-9]+\.txt$ {
        try_files $uri =404;
        access_log off;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name render.mengxa.com;

    ssl_certificate     /etc/nginx/ssl/render.mengxa.com/fullchain.pem;
    ssl_certificate_key /etc/nginx/ssl/render.mengxa.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    root /opt/OpenMontage/frameflow/bff/web;
    index index.html;

    # Gzip
    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml text/javascript image/svg+xml;
    gzip_min_length 256;

    # Static assets with long cache
    location ~* \.(?:css|js|png|jpg|jpeg|gif|ico|svg|woff2?)$ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    # BFF API 代理（BFF 在本地 8080 运行）
    location /api/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        client_max_body_size 50m;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }

    # Health check（转发给 BFF）
    location /health {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
    }

    # SPA fallback — 所有非文件路由返回 index.html
    location / {
        try_files $uri $uri/ /index.html;
    }

    # Security headers
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-XSS-Protection "1; mode=block" always;

    access_log /var/log/nginx/render.mengxa.com.access.log;
    error_log  /var/log/nginx/render.mengxa.com.error.log;
}
