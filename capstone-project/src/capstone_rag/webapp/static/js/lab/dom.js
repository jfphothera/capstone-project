// Minimal DOM helpers. Text is always inserted as text nodes, never as HTML.

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null) continue;
    if (key === 'class') node.className = value;
    else if (key.startsWith('on')) node[key.toLowerCase()] = value;
    else if (key === 'checked' || key === 'disabled' || key === 'hidden' || key === 'open') node[key] = Boolean(value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function replace(node, ...children) {
  node.replaceChildren(...children.flat().filter(child => child !== undefined && child !== null && child !== false));
}

export function setText(node, text) {
  const value = String(text ?? '');
  if (node.textContent !== value) node.textContent = value;
}

export function message(text, kind = '') {
  return el('div', { class: 'lab-message ' + kind }, text);
}

export function headNumber(resourceId) {
  return (resourceId || '').split('-').pop();
}
