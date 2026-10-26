/** Client preview of `domain.exports.export_filename` (spec section 8.3). */

const UNSAFE = new Set('/\\:*?"<>|');
const KEPT_FORMAT_CHARS = new Set(['‌', '‍']); // ZWNJ and ZWJ
const MAX_TITLE_BYTES = 150;
const ZS = /^\p{Zs}$/u;
const CC_OR_CF = /^[\p{Cc}\p{Cf}]$/u;

/** Characters that XML 1.0 forbids, including lone surrogates (iteration yields them alone). */
function isXmlInvalid(char: string): boolean {
  const cp = char.codePointAt(0) ?? 0;
  return (
    cp <= 0x08 ||
    cp === 0x0b ||
    cp === 0x0c ||
    (cp >= 0x0e && cp <= 0x1f) ||
    (cp >= 0xd800 && cp <= 0xdfff) ||
    cp === 0xfffe ||
    cp === 0xffff
  );
}

function clean(char: string): string {
  if (isXmlInvalid(char)) return '';
  if (UNSAFE.has(char)) return '_';
  if (char === '\t' || char === '\n' || char === '\r' || ZS.test(char)) return ' ';
  if (CC_OR_CF.test(char) && !KEPT_FORMAT_CHARS.has(char)) return '_';
  return char;
}

function cutToBytes(text: string, limit: number): string {
  const encoder = new TextEncoder();
  let bytes = 0;
  let out = '';
  for (const char of text) {
    bytes += encoder.encode(char).length;
    if (bytes > limit) break;
    out += char;
  }
  return out;
}

export function exportFilename(
  title: string | null,
  start: number,
  end: number,
  ext: string,
): string {
  let cleaned = Array.from((title ?? '').normalize('NFC'), clean).join('');
  cleaned = cleaned
    .replace(/ +/g, ' ')
    .replace(/^ +| +$/g, '')
    .replace(/^[. ]+/, '');
  const part = cutToBytes(cleaned, MAX_TITLE_BYTES).replace(/ +$/, '') || 'Untitled';
  return `${part} Chapters ${String(start)}-${String(end)}.${ext}`;
}
