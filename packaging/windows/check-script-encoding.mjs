/**
 * Deploy scripts must be parseable by Windows PowerShell 5.1 / cmd.
 *
 * PS 5.1 assumes ANSI (Windows-1252) for any .ps1 with no BOM. An em dash there
 * decodes to `a€”` -- and that trailing byte is U+201D, a SMART QUOTE, which the
 * parser treats as a string terminator. One em dash inside one string silently
 * made the whole file unparseable.
 *
 * It broke the standalone dashboard's self-updater from the commit that
 * introduced it: the backend spawns the updater DETACHED with no console, so
 * every failed update looked like "it said it would restart and nothing
 * happened". Keeping these files pure ASCII removes the encoding question
 * entirely -- cheaper than remembering to write a BOM.
 *
 *   node packaging/windows/check-script-encoding.mjs
 */
import { readdirSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const dir = dirname(fileURLToPath(import.meta.url));
let bad = 0;
for (const f of readdirSync(dir).filter((f) => /\.(ps1|bat|cmd)$/i.test(f))) {
  const raw = readFileSync(join(dir, f));
  const bom = raw[0] === 0xef && raw[1] === 0xbb && raw[2] === 0xbf;
  const text = raw.toString('utf8');
  const offenders = [...text].filter((c) => c.charCodeAt(0) > 127);
  if (offenders.length && !bom) {
    bad++;
    const uniq = [...new Set(offenders)].map((c) => `${JSON.stringify(c)} U+${c.charCodeAt(0).toString(16).toUpperCase().padStart(4, '0')}`);
    console.log(`FAIL  ${f}: ${offenders.length} non-ASCII char(s), no BOM -> ${uniq.join(', ')}`);
  } else {
    console.log(`ok    ${f}${bom ? ' (BOM)' : ''}`);
  }
}
console.log(bad ? `\nFAIL - ${bad} script(s) will misparse under PowerShell 5.1\n` : '\nPASS - deploy scripts are encoding-safe\n');
process.exit(bad ? 1 : 0);
