import { readFile } from "node:fs/promises";
import ts from "typescript";

export async function loadTs(name) {
  const source = await readFile(new URL(`../src/utils/${name}.ts`, import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ESNext },
  });
  return import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
}
