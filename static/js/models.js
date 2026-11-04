// アクティブなTOCリンクをハイライト。見るのは目次が指す見出しだけ——guide.html の用語集は項目ごとに
// id（#term-…）を持つので、id を全部見ると用語集の中で現在地が目次に無い id になり、どれも光らない
const tocLinks = document.querySelectorAll('.toc a');
const sections = [...tocLinks]
  .map(a => (a.getAttribute('href') || '').startsWith('#') && document.getElementById(a.getAttribute('href').slice(1)))
  .filter(Boolean);
let lastActive = null;
function updateToc() {
  let current = '';
  sections.forEach(s => {
    if (window.scrollY + 100 >= s.offsetTop) current = s.id;
  });
  let active = null;
  tocLinks.forEach(a => {
    const on = a.getAttribute('href') === '#' + current;
    a.classList.toggle('active', on);
    if (on) active = a;
  });
  if (active && active !== lastActive) keepInView(active);
  lastActive = active;
}
// 目次が枠の高さを超えるとき（guide.html は30項目近い）、光った項目が枠の外に出たら目次の中だけを
// スクロールして寄せる。scrollIntoView はページ本体まで動かすので使わない
function keepInView(link) {
  const toc = link.closest('.toc');
  if (!toc || toc.scrollHeight <= toc.clientHeight) return;
  const box = toc.getBoundingClientRect();
  const r = link.getBoundingClientRect();
  if (r.top < box.top) toc.scrollTop -= box.top - r.top + 8;
  else if (r.bottom > box.bottom) toc.scrollTop += r.bottom - box.bottom + 8;
}
// 他画面から /guide#… で飛んできたときは、読み込み後のアンカーへの移動で現在地が決まるので load と hashchange でも測る
['scroll', 'load', 'hashchange'].forEach(type => window.addEventListener(type, updateToc));
updateToc();

// 用語の吹き出し（.term）の位置合わせは common.js の placeTip が受け持つ（分析画面の .gloss と共有・#1137）
