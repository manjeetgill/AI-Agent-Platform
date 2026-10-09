import test from 'node:test';
import assert from 'node:assert/strict';
import {validRetailerCommand,validPairing,captureRetailer} from './retailer-policy.js';
const routes={croma:'https://www.croma.com/fixture/p/12345',jiomart:'https://www.jiomart.com/product/butter-100-g-12345',zepto:'https://www.zepto.com/pn/cream/pvid/490c9d61-d19e-462d-9022-6e8b5a0a03da',instamart:'https://instamart.in/p/butter-ABC'};
const command=(retailer='croma')=>({id:'12345678-1234-1234-1234-123456789abc',operation:'retailer.observe',retailer,url:routes[retailer],expires_at:new Date(Date.now()+300000).toISOString(),expected_pincode:'201306'});
function node(innerText='',selectors={}){return {innerText,getClientRects:()=>[{}],querySelectorAll:s=>selectors[s]||[],closest:()=>null};}
function fixture(c,selectors){const u=new URL(c.url);globalThis.location={origin:u.origin,pathname:u.pathname};globalThis.document={querySelectorAll:s=>selectors[s]||[]};globalThis.getComputedStyle=()=>({visibility:'visible'});globalThis.__workspaceStopped=false;}
test('only canonical allowed product routes and local pairing, no URL credentials or arbitrary JS',()=>{
 for(const source of Object.keys(routes))assert.ok(validRetailerCommand(command(source)));
 for(const override of [{url:routes.croma+'?token=secret'},{url:'https://www.croma.com.evil.test/a/p/12345'},{operation:'eval'},{expires_at:'2001-01-01'},{url:'https://www.croma.com/cart'},{expected_pincode:'invalid'}])assert.equal(validRetailerCommand({...command(),...override}),false);
 const p={api_base:'http://127.0.0.1:4060',pairing_code:'rbr_12345678-1234-1234-1234-123456789abc.'+'a'.repeat(43),retailers:['croma']};
 assert.ok(validPairing(p));assert.equal(validPairing({...p,api_base:'https://attacker.example'}),false);
});
test('Croma uses product price, not EMI; contradictory selected specification is flagged',()=>{
 const c=command();const h=node('Laptop 15.6 inches');const selected=node();selected.name='Display Size (In Inches)';selected.parentElement=node('15.3');
 const spec=node('Display Size (In Inches)');spec.parentElement={nextElementSibling:node('15.6 Inches')};
 fixture(c,{'h1':[h],'h1.pd-title':[h],'#pdp-product-price':[node('₹67,990.00')],'input[type="radio"]:checked':[selected],'h4':[spec],'.pincode.custom-s-pincode':[node('201306')]});
 const r=captureRetailer(c);assert.equal(r.price,67990);assert.ok(r.issues.includes('variant_conflict'));assert.equal(r.confirmed_pincode,'201306');
 globalThis.__workspaceStopped=true;assert.equal(captureRetailer(c).outcome,'INTERRUPTED');
});
test('JioMart mismatched pack sizes are unsafe even with delivery postcode',()=>{
 const c=command('jiomart');fixture(c,{'.product-description__productTitle':[node('Amul Butter 100 g')],'.PriceContainer__currentPrice':[node('₹62')],'table tr':[node('Net Quantity\t500 g')],'.DeliverySection__addressText':[node('Greater Noida, 201306')]});
 const r=captureRetailer(c);assert.equal(r.price,62);assert.equal(r.variant,'100 g');assert.ok(r.issues.includes('variant_conflict'));assert.equal(r.outcome,'OBSERVED');
});
test('Zepto reads split currency DOM, requires confirmed delivery location',()=>{
 const c=command('zepto');const h=node('Amul Fresh Cream');h.parentElement={parentElement:node('Amul\nAmul Fresh Cream\nNet Qty: 1 pc (1 L)\n4.7\n₹\n250\nMRP\n(incl taxes)')};fixture(c,{'h1':[h]});
 const r=captureRetailer(c);assert.equal(r.price,250);assert.equal(r.variant,'1 pc (1 L)');assert.equal(r.outcome,'LOCATION_REQUIRED');assert.ok(r.issues.includes('location_unconfirmed'));
});
test('Instamart scopes price away from recommended products and marks unconfirmed pack',()=>{
 const c=command('instamart');const details=node('100 g x 4\n252',{'[data-testid="item-offer-price"]':[node('252')]});
 fixture(c,{'h1':[node('Amul Butter')],'[data-testid="item-collection-pdp-price-details"]':[details],'[data-testid="item-offer-price"]':[node('82'),node('135')]});
 const r=captureRetailer(c);assert.equal(r.price,252);assert.ok(r.issues.includes('variant_unconfirmed'));assert.equal(r.outcome,'LOCATION_REQUIRED');
});
test('multiple prices, missing markup, redirect, expired and blocked pages fail closed',()=>{
 const c=command();fixture(c,{'h1':[node('Product')],'h1.pd-title':[node('Product')],'#pdp-product-price':[node('₹99'),node('₹199')]});
 assert.equal(captureRetailer(c).outcome,'PAGE_CHANGED');
 fixture(c,{'h1':[node('Access Denied')]});assert.equal(captureRetailer(c).outcome,'BLOCKED');
 fixture(c,{});assert.equal(captureRetailer(c).outcome,'PAGE_CHANGED');
 location.pathname='/login';assert.equal(captureRetailer(c).outcome,'PAGE_CHANGED');
});
