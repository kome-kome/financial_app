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

// 用語の吹き出し（guide.html の .term::after）は用語の左端から右へ伸びる。本文の右端を越える用語だけ
// .tip-flip で右端揃えにして、切れずに読めるようにする（#888）。外寸は CSS から読む——::after は
// * の box-sizing を受けないので、max-width に余白と枠線を足したものが最大の外寸。
// 狭い本文では右端揃えでも左端を越えるので、外寸を本文の幅までに抑え（--tip-room）、越える分だけ
// 右へ戻す（--tip-shift）。ホバーとフォーカス（Tab・タップ）の両方で開くので両方で測る（#893）
function placeTip(term) {
  const box = (term.closest('.content') || document.documentElement).getBoundingClientRect();
  const tip = getComputedStyle(term, '::after');
  const frame = ['paddingLeft', 'paddingRight', 'borderLeftWidth', 'borderRightWidth']
    .reduce((sum, prop) => sum + parseFloat(tip[prop]), 0);
  term.style.setProperty('--tip-room', `${box.width - frame}px`);
  const tipWidth = parseFloat(tip.maxWidth) + frame;  // tip は live なので --tip-room を反映した値
  const rect = term.getBoundingClientRect();
  const flip = rect.left + tipWidth > box.right;
  term.classList.toggle('tip-flip', flip);
  term.style.setProperty('--tip-shift', `${flip ? Math.max(0, box.left + tipWidth - rect.right) : 0}px`);
}
['mouseover', 'focusin'].forEach(type => document.addEventListener(type, e => {
  const term = e.target.closest('.term');
  if (term) placeTip(term);
}));
