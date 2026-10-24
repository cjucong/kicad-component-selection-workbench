"""Selection editing menu regression using actual Chromium context-menu input."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_selected_text_menu_and_input_selection():
    node = shutil.which('node')
    if not node or subprocess.run([node,'-e',"require.resolve('playwright')"],capture_output=True).returncode:
        pytest.skip('Node.js/Playwright required')
    script = r'''
const {chromium}=require('playwright'), assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:800,height:600}}), errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.setContent('<p id="text">Selected text QA</p><input id="edit" value="100nF"><input id="read" readonly value="Read only"><div id="content" contenteditable>Notes</div><button id="ref">R1</button><dialog><p id="inside">Dialog text QA</p></dialog>');
  await page.evaluate(()=>{window.requests=[];window.pywebview={api:{workbench_edit:context=>requests.push(context)}};window.refMenu=0;document.querySelector('#ref').oncontextmenu=e=>{e.preventDefault();window.refMenu++}});
  await page.addScriptTag({path:process.env.EDIT_MENU_JS});
  const select=async id=>page.evaluate(id=>{const r=document.createRange();r.selectNodeContents(document.querySelector(id));getSelection().removeAllRanges();getSelection().addRange(r)},id);
  await select('#text'); await page.locator('#text').click({button:'right'});
  assert.deepEqual(await page.evaluate(()=>[requests.length,requests.at(-1).selected,requests.at(-1).writable]),[1,true,false]);
  assert.equal(await page.evaluate(()=>getSelection().toString()),'Selected text QA');
  await page.evaluate(()=>{getSelection().removeAllRanges();const el=document.querySelector('#edit');el.focus();el.setSelectionRange(1,4)});
  await page.locator('#edit').click({button:'right',position:{x:20,y:10}});
  assert.deepEqual(await page.evaluate(()=>[document.activeElement.id,document.activeElement.selectionStart,document.activeElement.selectionEnd]),['edit',1,4]);
  assert.deepEqual(await page.evaluate(()=>[requests.filter(r=>!r.dismiss).length,requests.at(-1).selected,requests.at(-1).writable]),[2,true,true]);
  await page.locator('#read').click({button:'right'});
  assert.equal(await page.evaluate(()=>requests.at(-1).writable),false);
  await page.evaluate(()=>document.querySelector('dialog').showModal());
  await select('#inside'); await page.locator('#inside').click({button:'right'});
  assert.deepEqual(await page.evaluate(()=>[requests.filter(r=>!r.dismiss).length,requests.at(-1).selected,requests.at(-1).writable]),[4,true,false]);
  await page.evaluate(()=>{document.querySelector('dialog').close();getSelection().removeAllRanges()});
  await page.locator('#ref').click({button:'right'});
  assert.equal(await page.evaluate(()=>refMenu),1,'Keep reference splitting menu with no selection');
  await page.locator('#content').click({button:'right'});
  assert.equal(await page.evaluate(()=>requests.at(-1).writable),true);
  await page.mouse.click(750,550);
  assert.equal(await page.evaluate(()=>requests.at(-1).dismiss),true,'Blank click must dismiss the native menu');
  const dismissCount=await page.evaluate(()=>requests.length);
  await page.mouse.click(750,550);
  assert.equal(await page.evaluate(()=>requests.length),dismissCount,'No duplicate close requests without a menu');
  await page.locator('#content').click({button:'right'});
  await page.evaluate(()=>window.dispatchEvent(new Event('resize')));
  assert.equal(await page.evaluate(()=>requests.at(-1).dismiss),true);
  await page.locator('#content').click({button:'right'});
  await page.evaluate(()=>window.dispatchEvent(new Event('blur')));
  assert.equal(await page.evaluate(()=>requests.at(-1).dismiss),true,'Blur must cancel even a pending menu request');
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
  assert.equal(await page.evaluate(()=>requests.at(-1).dismiss),true,'Focus does not reopen a cancelled menu');
  assert.equal(await page.evaluate(()=>requests.every((r,i)=>r.generation===0 && r.sequence===i+1)),true);
  const count=await page.evaluate(()=>requests.length);
  await page.evaluate(()=>document.querySelector('#content').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true})));
  assert.equal(await page.evaluate(()=>requests.length),count,'Synthetic events cannot request editing menus');
  assert.deepEqual(errors,[]);
 } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run([node,'-e',script],env={**os.environ,'EDIT_MENU_JS':str(
        Path(__file__).resolve().parents[1]/'tools/web/edit-menu.js')},capture_output=True,text=True,timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
