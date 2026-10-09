// アクティブなTOCリンクをハイライト
const sections = document.querySelectorAll('[id]');
const tocLinks = document.querySelectorAll('.toc a');
function updateToc() {
  let current = '';
  sections.forEach(s => {
    if (window.scrollY + 100 >= s.offsetTop) current = s.id;
  });
  tocLinks.forEach(a => {
    a.classList.toggle('active', a.getAttribute('href') === '#' + current);
  });
}
window.addEventListener('scroll', updateToc);
updateToc();

// 用語の吹き出し（guide.html の .term::after）は用語の左端から右へ伸びる。本文の右端を越える用語だけ
// .tip-flip で右端揃えにして、切れずに読めるようにする（#888）。外寸は CSS から読む——::after は
// * の box-sizing を受けないので、max-width に余白と枠線を足したものが最大の外寸
document.addEventListener('mouseover', e => {
  const term = e.target.closest('.term');
  if (!term) return;
  const tip = getComputedStyle(term, '::after');
  const tipWidth = ['maxWidth', 'paddingLeft', 'paddingRight', 'borderLeftWidth', 'borderRightWidth']
    .reduce((sum, prop) => sum + parseFloat(tip[prop]), 0);
  const right = (term.closest('.content') || document.documentElement).getBoundingClientRect().right;
  term.classList.toggle('tip-flip', term.getBoundingClientRect().left + tipWidth > right);
});
