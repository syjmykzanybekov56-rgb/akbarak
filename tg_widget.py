"""
АкБарак — интерактивная карточка заказа в Telegram (HTML-виджет, Bot API 10.4).

Виджет — маленькая страница внутри сообщения бота. Он рисует заказ из chat_data (JSON),
а нажатия отправляет боту через TelegramWidget.request("код"). Бот меняет заказ и
присылает новое состояние через editMessageWidgetData — виджет перерисовывается сам.

Ограничения Telegram: только личный чат с ботом, свежие версии приложения, HTML до 4 КБ
в сжатом виде, без сети и без ввода текста. Поэтому оплату бот присылает отдельным сообщением.
"""

import json
import zlib

TXT = {
    "ru": {"pg": "стр.", "bw": "Ч/Б", "col": "Цвет", "s1": "1 сторона", "s2": "2 стороны", "bk": "Брошюра",
           "doc": "Документ A4", "ph": "Фото 10×15", "idp": "Фото на документы · 50 сом", "cp": "Копий",
           "pay": "Оплатить", "free": "Бесплатно", "x": "Отменить", "som": "сом", "w": "Ждём оплату — кнопка ниже",
           "p": "Оплачен", "r": "Печатается", "ok": "Готово", "xx": "Заказ отменён", "f": "Ошибка печати",
           "o": "Заказ №", "fmt": "Формат", "md": "Печать", "sd": "Стороны", "term": "Терминал"},
    "ky": {"pg": "бет", "bw": "Ак-кара", "col": "Түстүү", "s1": "1 жагы", "s2": "2 жагы", "bk": "Брошюра",
           "doc": "Документ A4", "ph": "Сүрөт 10×15", "idp": "Документке сүрөт · 50 сом", "cp": "Нуска",
           "pay": "Төлөө", "free": "Акысыз", "x": "Жокко чыгаруу", "som": "сом", "w": "Төлөм күтүлүүдө — баскыч төмөндө",
           "p": "Төлөндү", "r": "Басылууда", "ok": "Даяр", "xx": "Буйрутма жокко чыкты", "f": "Басуу катасы",
           "o": "Буйрутма №", "fmt": "Формат", "md": "Басуу", "sd": "Тараптар", "term": "Терминал"},
    "en": {"pg": "p.", "bw": "B/W", "col": "Colour", "s1": "1 side", "s2": "2 sides", "bk": "Booklet",
           "doc": "A4 document", "ph": "Photo 10×15", "idp": "ID photos · 50 som", "cp": "Copies",
           "pay": "Pay", "free": "Free", "x": "Cancel", "som": "som", "w": "Waiting for payment — button below",
           "p": "Paid", "r": "Printing", "ok": "Done", "xx": "Order cancelled", "f": "Print error",
           "o": "Order #", "fmt": "Format", "md": "Print", "sd": "Sides", "term": "Terminal"},
}

# Состояние (chat_data, ≤1024 байт). Короткие ключи, чтобы влезть:
# n имя, p стр., c копий, k цвет 0/1, K можно цвет, s стороны, D можно 2 стороны, i картинка,
# f фото (''|10x15|passport|d30x40|d25x35|d50x50), t сумма, o владелец, z статус, id заказ, m терминал, v ревизия
HTML = r"""<style>
*{box-sizing:border-box;margin:0}
body{font:14px/1.3 system-ui,sans-serif;color:var(--tg-theme-text-color);padding:12px}
h3{font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.m{color:var(--tg-theme-hint-color);font-size:12px;margin:3px 0 4px}
.l{font-size:11px;color:var(--tg-theme-hint-color);text-transform:uppercase;letter-spacing:.04em;margin:10px 0 5px}
.r{display:flex;flex-wrap:wrap;gap:6px}
.b{flex:1 1 auto;border:0;border-radius:11px;padding:9px 8px;background:var(--tg-theme-secondary-bg-color);color:inherit;font:inherit;font-size:13px}
.on{background:#E0B44C;color:#14283D;font-weight:700}
.q{display:flex;gap:8px;align-items:center;margin-top:12px}
.q .b{flex:0 0 42px;font-size:18px;padding:6px}
.n{min-width:26px;text-align:center;font-weight:700;font-size:16px}
.go{flex:1 1 auto!important;background:#2E9E5B;color:#fff;font-weight:700;font-size:15px!important;padding:11px!important}
.fr{width:100%;margin-top:6px;background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color);font-weight:700}
.x{width:100%;margin-top:6px;background:none;color:#D9534F;font-size:13px}
.pg{display:flex;gap:4px;margin:12px 0 5px}.pg i{flex:1;height:5px;border-radius:3px;background:var(--tg-theme-secondary-bg-color)}.pg i.a{background:#E0B44C}
.ps{display:flex;justify-content:space-between;font-size:11px;color:var(--tg-theme-hint-color)}
.st{margin-top:10px;padding:10px;border-radius:11px;background:var(--tg-theme-secondary-bg-color);font-weight:700}
.st small{display:block;font-weight:400;color:var(--tg-theme-hint-color);margin-top:2px}
.dim{opacity:.4}
</style><div id=a></div><script>
var W=window.TelegramWidget,L=__TXT__,S={},busy=0;
function E(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function B(t,c,on,cl){return'<button class="b'+(on?' on':'')+(cl?' '+cl:'')+'" data-c="'+c+'">'+t+'</button>'}
function R(){try{S=JSON.parse(W?W.chatData:'')||{}}catch(e){S={}}
var h='<h3>'+E(S.n)+'</h3><div class=m>'+(S.id?L.o+S.id+' · ':'')+S.p+' '+L.pg+' · '+L.term+' '+E(S.m)+'</div>',z=S.z||'d';
if(z!='d'){var k={w:0,p:1,r:2,ok:3}[z];
if(k!=null){h+='<div class=pg>'+[1,2,3].map(function(i){return'<i'+(i<=k?' class=a':'')+'></i>'}).join('')+'</div><div class=ps><span>'+L.p+'</span><span>'+L.r+'</span><span>'+L.ok+'</span></div>'}
h+='<div class=st>'+({w:'💳 '+L.w,p:'✅ '+L.p,r:'🖨 '+L.r,ok:'🎉 '+L.ok,x:L.xx,f:'⚠ '+L.f}[z]||'')+'<small>'+S.t+' '+L.som+'</small></div>'}
else{var f=S.f||'';
if(S.i){h+='<div class=l>'+L.fmt+'</div><div class=r>'+B('📄 '+L.doc,'f:',!f)+B('🖼 '+L.ph+' · 15','f:10x15',f=='10x15')+'</div><div class=l>'+L.idp+'</div><div class=r>'+
[['d30x40','3×4'],['passport','3.5×4.5'],['d25x35','2.5×3.5'],['d50x50','5×5']].map(function(x){return B(x[1],'f:'+x[0],f==x[0])}).join('')+'</div>'}
if(!f){if(S.K)h+='<div class=l>'+L.md+'</div><div class=r>'+B('⚫ '+L.bw,'k0',!S.k)+B('🎨 '+L.col,'k1',S.k)+'</div>';
if(S.D)h+='<div class=l>'+L.sd+'</div><div class=r>'+B(L.s1,'s:single',S.s=='single')+B(L.s2,'s:duplex',S.s=='duplex')+B(L.bk,'s:booklet',S.s=='booklet')+'</div>'}
h+='<div class=q>'+B('−','c-',0,S.c<=1?'dim':'')+'<span class=n>'+S.c+'</span>'+B('+','c+',0,S.c>=20?'dim':'')+B(L.pay+' · '+S.t+' '+L.som,'pay',0,'go')+'</div>';
if(S.o)h+=B('🆓 '+L.free,'free',0,'fr');h+=B('✕ '+L.x,'x',0,'x')}
document.getElementById('a').innerHTML=h}
document.addEventListener('click',function(ev){var b=ev.target.closest('[data-c]');if(!b||!W||busy||(S.z&&S.z!='d'))return;busy=1;
if(W.HapticFeedback)W.HapticFeedback.selectionChanged();
W.request(b.dataset.c).then(function(){busy=0},function(){busy=0})});
if(W){W.onEvent('chatDataChanged',R);R();W.ready()}else R();
</script>"""

_CACHE = {}


def html(lang):
    lang = lang if lang in TXT else "ru"
    if lang not in _CACHE:
        _CACHE[lang] = HTML.replace("__TXT__", json.dumps(TXT[lang], ensure_ascii=False, separators=(",", ":")))
    return _CACHE[lang]


def size_ok(lang):
    h = html(lang).encode("utf-8")
    return len(h) <= 32768 and len(zlib.compress(h, 9)) <= 4096


def state(d, total, owner, color_ok, duplex_ok, z="d", order_id=None, rev=0):
    is_img = (d.get("file_path") or "").lower().endswith((".jpg", ".jpeg", ".png"))
    s = {"n": (d.get("file_name") or "")[:60], "p": d.get("pages") or 1, "c": d.get("copies") or 1,
         "k": 1 if d.get("color") else 0, "K": 1 if color_ok else 0, "s": d.get("sides") or "single",
         "D": 1 if (duplex_ok and (d.get("pages") or 1) > 1 and not d.get("photo")) else 0,
         "i": 1 if (is_img and color_ok) else 0, "f": d.get("photo") or "", "t": total, "o": 1 if owner else 0,
         "z": z, "m": d.get("terminal") or "", "v": rev}
    if order_id:
        s["id"] = order_id
    return json.dumps(s, ensure_ascii=False, separators=(",", ":"))
