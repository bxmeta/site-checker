# Развёртывание site-checker

Эталон: Ubuntu 24.04, 1 vCPU, 1 ГБ RAM, Python 3.12, nginx, acme.sh, ufw.

```bash
# пользователь и каталоги
useradd -r -m -d /opt/site-checker -s /usr/sbin/nologin sitemon
mkdir -p /opt/site-checker/app && chown -R sitemon:sitemon /opt/site-checker && chmod 750 /opt/site-checker

# код и окружение
rsync -az --exclude .git --exclude __pycache__ ./ /opt/site-checker/app/
sudo -u sitemon python3 -m venv /opt/site-checker/venv
sudo -u sitemon /opt/site-checker/venv/bin/pip install -r /opt/site-checker/app/requirements.txt

# данные: config.yaml (секреты!), b24_auth.json
install -o sitemon -g sitemon -m 600 config.yaml /opt/site-checker/config.yaml

# ping без привилегий (диагностика падений)
install -m 644 deploy/90-site-monitor.sysctl.conf /etc/sysctl.d/ && sysctl -p /etc/sysctl.d/90-site-monitor.conf

# корень Let's Encrypt "ISRG Root YR" (цепочка YR1), иначе такие сайты дают ssl_untrusted
curl -sS -o /tmp/root-yr.pem https://letsencrypt.org/certs/gen-y/root-yr.pem
openssl x509 -in /tmp/root-yr.pem -noout -fingerprint -sha256   # E5:7B:7E:6F:15:0C:41:91:02:E8:D5:C0:55:72:9F:F9:67:B9:D1:A8:29:BF:00:CE:C8:9C:A6:04:EB:F4:A8:6F
cp /tmp/root-yr.pem /usr/local/share/ca-certificates/ISRG-Root-YR.crt && update-ca-certificates

# служба
install -m 644 deploy/site-monitor.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now site-monitor

# nginx для событий Bitrix24 (порт 2053, сертификат acme.sh)
install -m 644 deploy/nginx-monitor-bot.conf /etc/nginx/sites-enabled/monitor-bot   # поправить server_name
ufw allow 2053/tcp && nginx -t && systemctl reload nginx
```

Проверка: `curl 127.0.0.1:8000/b24/health`, лог `/opt/site-checker/monitor.log`, `journalctl -u site-monitor`.

Обновление кода: `rsync` новой версии в `/opt/site-checker/app` (исключая `config.yaml`, `*.db`, `*.log`,
`b24_auth.json`), затем `systemctl restart site-monitor`.

Память: без Telegram-бота ~60 МБ, с aiogram ~200 МБ (импорт aiogram 3 стоит ~150 МБ).
Лимиты в юните: MemoryHigh=200M, MemoryMax=256M.
