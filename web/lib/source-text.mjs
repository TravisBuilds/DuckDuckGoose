/**
 * Reading the web app's own source for the copy tests (lib/home-copy.test.mjs, lib/app-copy.test.mjs):
 * the strings a file writes, whether each is text someone reads, and every file a page is built from.
 */
import { existsSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

export const webDir = fileURLToPath(new URL('..', import.meta.url));

const parse = (file) =>
  ts.createSourceFile(file, readFileSync(join(webDir, file), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);

/**
 * Every string literal and piece of JSX text in a source file, outside className attributes and
 * imports, with whether it is visible text (JSX text, a string in JSX children or a text attribute,
 * or prose), whether it is rendered as a JSX child, and whether that JSX text sits between two values
 * (the dash in `{min}–{max}s`).
 */
export function literalsIn(file) {
  const TEXT_ATTRIBUTES = new Set(['alt', 'title', 'label', 'placeholder', 'aria-label', 'aria-description']);
  const NOT_TEXT_ATTRIBUTES = new Set(['className', 'sizes', 'srcSet', 'd', 'viewBox']);
  const PROSE = /\p{L}{2,}[\s,.!?]+\p{L}{2,}/u;
  const source = parse(file);
  const found = [];
  const add = (node, text, visible, child, between = false) => {
    const { line } = source.getLineAndCharacterOfPosition(node.getStart());
    found.push({ path: `${file}:${line + 1}`, text, visible, child, between });
  };
  const textOf = (node) => {
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
    if (ts.isTemplateExpression(node)) return [node.head.text, ...node.templateSpans.map((span) => span.literal.text)].join(' ');
    return null;
  };
  /** A literal rendered as a JSX child, directly or as a branch of a conditional or && there. */
  const inJsxChildren = (node) => {
    let at = node;
    while (ts.isConditionalExpression(at.parent) || ts.isParenthesizedExpression(at.parent)
      || (ts.isBinaryExpression(at.parent) && at.parent.right === at)) at = at.parent;
    return ts.isJsxExpression(at.parent) && (ts.isJsxElement(at.parent.parent) || ts.isJsxFragment(at.parent.parent));
  };
  const visit = (node, attribute) => {
    if (ts.isImportDeclaration(node)) return;
    if (ts.isJsxAttribute(node)) {
      const name = node.name.getText(source);
      if (name === 'className') return;
      ts.forEachChild(node, (child) => visit(child, name));
      return;
    }
    if (ts.isJsxText(node)) {
      if (node.text.trim()) {
        const siblings = node.parent.children ?? [];
        const at = siblings.indexOf(node);
        add(node, node.text.trim(), /\p{L}/u.test(node.text), true, at > 0 && at < siblings.length - 1);
      }
      return;
    }
    const text = textOf(node);
    if (text !== null) {
      const child = attribute === undefined && inJsxChildren(node);
      const visible = (attribute !== undefined && TEXT_ATTRIBUTES.has(attribute) && /\p{L}/u.test(text))
        || (child && /\p{L}/u.test(text))
        || (!NOT_TEXT_ATTRIBUTES.has(attribute) && PROSE.test(text));
      add(node, text, visible, child);
    }
    ts.forEachChild(node, (child) => visit(child, attribute));
  };
  visit(source, undefined);
  return found;
}

/** The JSX tags a source file renders, with where. */
export function tagsIn(file) {
  const source = parse(file);
  const found = [];
  const visit = (node) => {
    if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      const { line } = source.getLineAndCharacterOfPosition(node.getStart());
      found.push({ path: `${file}:${line + 1}`, tag: node.tagName.getText(source) });
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

/** The modules a source file imports or re-exports, as written. */
export function importsOf(file) {
  const source = parse(file);
  const specifiers = [];
  const visit = (node) => {
    if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
      specifiers.push(node.moduleSpecifier.text);
    }
    if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword && ts.isStringLiteral(node.arguments[0] ?? {})) {
      specifiers.push(node.arguments[0].text);
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return specifiers;
}

const EXTENSIONS = ['', '.tsx', '.ts', '.mjs', '.js', '/index.tsx', '/index.ts', '/index.mjs', '/index.js'];

/** A file of this app an import resolves to (`@/` is web/), or null for a package, a stylesheet or an asset. */
function resolveImport(from, specifier) {
  let base;
  if (specifier.startsWith('@/')) base = specifier.slice(2);
  else if (specifier.startsWith('.')) base = join(dirname(from), specifier);
  else return null;
  for (const extension of EXTENSIONS) {
    const file = base + extension;
    const path = join(webDir, file);
    if (existsSync(path) && statSync(path).isFile() && /\.(?:tsx?|mjs|js)$/.test(file)) return relative(webDir, path);
  }
  return null;
}

/** Every source file of this app the given files import, directly or through others, the given files included. */
export function sourcesFrom(entries) {
  const seen = new Set();
  const queue = [...entries];
  while (queue.length > 0) {
    const file = queue.shift();
    if (seen.has(file)) continue;
    seen.add(file);
    for (const specifier of importsOf(file)) {
      const target = resolveImport(file, specifier);
      if (target && !seen.has(target)) queue.push(target);
    }
  }
  return [...seen].sort();
}
