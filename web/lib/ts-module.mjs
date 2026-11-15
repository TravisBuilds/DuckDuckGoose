/**
 * Test helper: load an import-free TypeScript lib file (lib/time.ts, lib/labels.ts) in node --test.
 * CI runs Node 20, which cannot import .ts, so the file is transpiled with the project's TypeScript.
 */
import { readFileSync } from 'node:fs';
import ts from 'typescript';

export async function importTs(url) {
  const source = readFileSync(url, 'utf-8');
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    fileName: url.pathname,
  });
  return import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);
}
