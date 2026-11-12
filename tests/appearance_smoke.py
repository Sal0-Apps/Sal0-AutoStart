"""Browser regression for the real appearance component, without app services."""
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re,threading,shutil,os
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
def assets():
    for directory in ('app/static','panel/static'):
        p=ROOT/directory
        if (p/'appearance.js').exists():return (p/'appearance.js').read_text(),(p/'appearance.css').read_text()
    p=ROOT/('app/templates/index.html' if (ROOT/'app/templates/index.html').exists() else 'index.html')
    html=p.read_text()
    script=html[html.index('/* Sal0 native themes:'):].split('</script>')[0]
    css=html[html.index('/* Sal0 glass themes.'):].split('</style>')[0]
    return script,css

SCRIPT,CSS=assets()
config=__import__('json').loads(re.search(r'const config = (\{[^\n]+\});',SCRIPT)[1])
native="""const c=CONFIG;function apply(m){document.documentElement.dataset.theme=m;if(c.source==='body')document.body.classList.toggle('light',m==='light');localStorage.setItem(c.key,m);}apply(localStorage.getItem(c.key)||'dark');document.getElementById(c.button).onclick=()=>apply(document.documentElement.dataset.theme==='light'?'dark':'light');""".replace('CONFIG',__import__('json').dumps(config))
HTML=f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/base.css"><link rel="stylesheet" href="/appearance.css"><script defer src="/native.js"></script><script defer src="/appearance.js"></script></head><body><button id="{config['button']}" type="button">Claro/escuro</button><div class="card">Conteúdo para rolagem</div></body></html>'''
FILES={'/':(HTML,'text/html'),'/appearance.js':(SCRIPT,'text/javascript'),'/appearance.css':(CSS,'text/css'),'/native.js':(native,'text/javascript'),'/base.css':('body{margin:0;background:rgb(11,22,33)}.card{margin:30px;min-height:2200px;background:rgb(22,33,44)}','text/css')}
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        body,mime=FILES.get(self.path,('','text/plain'));data=body.encode();self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(data)));self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:");self.end_headers();self.wfile.write(data)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    with sync_playwright() as p:
        executable=os.getenv('SAL0_BROWSER_EXECUTABLE') or shutil.which('google-chrome')
        browser=p.chromium.launch(headless=True,executable_path=executable)
        page=browser.new_page(viewport={'width':390,'height':844});errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(f'http://127.0.0.1:{server.server_port}/')
        page.get_by_role('button',name='Escolher tema visual',exact=True).click()
        page.get_by_role('radio',name='Tema antigo',exact=False).click()
        assert page.locator('html').get_attribute('data-sal0-look')=='legacy'
        assert page.locator('#sal0-scene').evaluate('e=>getComputedStyle(e).display')=='none'
        assert page.locator('.card').evaluate('e=>getComputedStyle(e).backgroundColor')=='rgb(22, 33, 44)'
        for look in ('Cristal','Aurora','Wall'):
            page.get_by_role('radio',name=look,exact=False).click()
            assert page.locator('#sal0-scene').is_visible()
        page.get_by_role('button',name='Claro',exact=True).click()
        assert page.locator('html').get_attribute('data-sal0-mode')=='light'
        assert page.locator('meta[name="color-scheme"]').get_attribute('content')=='only light'
        page.get_by_role('switch',name='Animações suaves',exact=False).uncheck()
        assert page.locator('#sal0-scene').get_attribute('data-paused')=='true'
        page.get_by_role('switch',name='Animações suaves',exact=False).check()
        page.get_by_role('button',name='Fechar temas',exact=True).click()
        page.evaluate('window.appearanceSvg=document.querySelector("#sal0-scene svg")')
        before=page.locator('#sal0-scene').bounding_box()
        time=page.evaluate('appearanceSvg.getCurrentTime()')
        page.evaluate('scrollTo(0,1000)')
        page.wait_for_function('scrollY>0')
        after=page.locator('#sal0-scene').bounding_box()
        assert before['y']==after['y']==0 and before['height']==after['height'],(before,after)
        assert page.evaluate('appearanceSvg===document.querySelector("#sal0-scene svg")&&appearanceSvg.getCurrentTime()>=0')
        assert page.evaluate('appearanceSvg.getCurrentTime()')>=time
        page.reload();page.wait_for_selector('#sal0-scene')
        assert page.locator('html').get_attribute('data-sal0-look')=='wall'
        assert page.locator('html').get_attribute('data-sal0-mode')=='light'
        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
        assert not errors,errors
        browser.close()
        print('Appearance OK: original styles, four looks, native modes, persistence, motion and stable scrolling under CSP.')
finally:server.shutdown();server.server_close()
