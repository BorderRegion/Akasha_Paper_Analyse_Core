/** Enforce the documented single API path and keep fixtures out of shipped code. */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src');
const problems = [];
let checked = 0;
function visitDirectory(directory) {
  for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
    const file = path.join(directory, item.name);
    if (item.isDirectory()) { visitDirectory(file); continue; }
    if (!/\.tsx?$/.test(file)) continue;
    const relative = path.relative(root, file).replaceAll(path.sep, '/');
    if (relative.startsWith('testing/')) continue;
    checked++;
    const source = ts.createSourceFile(file, fs.readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true);
    function inspect(node) {
      if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier
          && ts.isStringLiteral(node.moduleSpecifier)) {
        const specifier = node.moduleSpecifier.text;
        if (/(^|\/)(testing|fixtures|tests)(\/|$)/.test(specifier)) {
          problems.push(`${relative}: production code imports test data: ${specifier}`);
        }
      }
      if (/^(features|components)\//.test(relative) && ts.isCallExpression(node)) {
        const callee = node.expression;
        if ((ts.isIdentifier(callee) && callee.text === 'fetch')
            || (ts.isPropertyAccessExpression(callee) && callee.name.text === 'fetch')) {
          problems.push(`${relative}: use the shared API client instead of direct fetch`);
        }
      }
      ts.forEachChild(node, inspect);
    }
    inspect(source);
  }
}
visitDirectory(root);
if (problems.length) {
  console.error(problems.join('\n'));
  process.exitCode = 1;
} else console.log(`Boundary checks passed: ${checked} source files`);
