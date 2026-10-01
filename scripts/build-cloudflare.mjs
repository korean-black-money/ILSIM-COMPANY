import {build} from 'esbuild';
await build({entryPoints:['cloudflare/worker.js'],outfile:'_worker.js',bundle:true,format:'esm',platform:'browser',target:'es2022',loader:{'.html':'text'}});
console.log('Built Cloudflare Pages worker.');
