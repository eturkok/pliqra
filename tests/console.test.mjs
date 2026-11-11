import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';

// Load the actual console helpers without starting its browser/network lifecycle.
const source=readFileSync(new URL('../pliqra/static/app.js',import.meta.url),'utf8').split('\ninit();')[0];
const context=vm.createContext({document:{querySelector:()=>null,addEventListener(){}},window:{addEventListener(){}}});
vm.runInContext(source,context);
for(const helper of ['field','selectField'])test(`${helper} encodes imported reference names in attributes`,()=>{
  const reference='map_"><img src=x onerror="alert(1)">';
  const expression=helper==='field'?`field("Mapping",${JSON.stringify(reference)})`:`selectField("Mapping",${JSON.stringify(reference)},[])`;
  const html=vm.runInContext(expression,context);
  assert.ok(!html.includes('<img'),'Untrusted metadata introduced an HTML element');
  assert.ok(!html.includes('onerror="'),'Untrusted metadata escaped its attribute');
});
test('normal import mapping names remain unchanged',()=>{
  assert.match(vm.runInContext('selectField("Mapping","map_dep_1",[])',context),/name="map_dep_1"/);
});
