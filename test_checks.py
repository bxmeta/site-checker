"""Прогон новых проверок по всем сайтам конфига без уведомлений и записи в БД."""
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from monitor.config_loader import load_config, SiteConfig
from monitor.checker import check_site
from monitor.diagnostics import diagnose
from monitor.messages import format_down_message, format_ssl_expiring_message

async def main():
    cfg = load_config(os.path.join(os.path.dirname(__file__), "config.yaml"))
    print(f"ssl cfg: {cfg.ssl}; concurrency={cfg.scheduler.concurrency}")
    t = time.time()
    sem = asyncio.Semaphore(10)
    async def one(site):
        async with sem:
            return await check_site(site, cfg.default, cfg.ssl)
    results = await asyncio.gather(*(one(s) for s in cfg.sites))
    print(f"all checks in {time.time()-t:.1f}s\n")
    for site, r in zip(cfg.sites, results):
        ssl = r.ssl
        ssl_s = f" | SSL {ssl.issuer_str} до {ssl.expiry_str} ({ssl.days_until_expiry} дн.) auto_renew={ssl.auto_renew} trusted={ssl.chain_trusted}" if ssl else ""
        print(f"{'OK ' if r.success else 'ERR'} {site.name:28} code={r.status_code} {r.response_time_ms}ms type={r.error_type} err={r.error}{ssl_s}")
    # диагностика для упавших
    for site, r in zip(cfg.sites, results):
        if not r.success:
            r.diagnostics = await diagnose(site.url, cfg.default.timeout_seconds)
            print("\n" + format_down_message(site, r, lambda x: f"*{x}*"))
    # пример предупреждения SSL для платного сертификата
    for site, r in zip(cfg.sites, results):
        if r.ssl and r.ssl.valid and not r.ssl.auto_renew:
            print("\n" + format_ssl_expiring_message(site, r.ssl, lambda x: f"*{x}*")); break
    # синтетические случаи
    extra = [
        SiteConfig(id="t_expired", name="expired.badssl.com", url="https://expired.badssl.com"),
        SiteConfig(id="t_selfsigned", name="self-signed.badssl.com", url="https://self-signed.badssl.com"),
        SiteConfig(id="t_wronghost", name="wrong.host.badssl.com", url="https://wrong.host.badssl.com"),
        SiteConfig(id="t_incomplete", name="incomplete-chain.badssl.com", url="https://incomplete-chain.badssl.com"),
        SiteConfig(id="t_nxdomain", name="nxdomain", url="https://definitely-not-a-domain-xyz123.ru"),
        SiteConfig(id="t_refused", name="refused", url="http://127.0.0.1:9/"),
        SiteConfig(id="t_timeout", name="blackhole", url="https://10.255.255.1/"),
        SiteConfig(id="t_500", name="httpstat 503", url="https://httpstat.us/503"),
    ]
    print("\n=== синтетика")
    res = await asyncio.gather(*(check_site(s, cfg.default, cfg.ssl) for s in extra))
    for s, r in zip(extra, res):
        print(f"{'OK ' if r.success else 'ERR'} {s.name:28} code={r.status_code} type={r.error_type} err={r.error}")
    d = await diagnose("https://10.255.255.1/", 5); print("diag blackhole:", d)
    d = await diagnose("https://incomplete-chain.badssl.com/", 10); print("diag incomplete:", d)

asyncio.run(main())
