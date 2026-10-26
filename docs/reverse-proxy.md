# Reverse proxy

Terminate HTTPS in front of novelhog (service workers and push require it). The proxy must pass the original
`Host` header (including a non-default port) and must not buffer `/api/v1/events`.

## Caddy

```caddyfile
novelhog.example.com {
	reverse_proxy 127.0.0.1:8080 {
		flush_interval -1
	}
}
```

## nginx

```nginx
server {
    listen 443 ssl;
    server_name novelhog.example.com;
    # ssl_certificate / ssl_certificate_key ...

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/v1/events {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 1h;
    }
}
```

`$http_host` (not `$host`) keeps the port, so the API's origin check passes when the site is served on a
non-default port.

Set `NOVELHOG_PUBLIC_URL` to the proxy's external URL so the host check lets it through.
