import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

function singleFile(): Plugin {
  return {
    name: 'longhub-single-file',
    enforce: 'post',
    generateBundle: {
      order: 'post',
      handler(_, bundle) {
        const html = bundle['index.html'];
        if (!html || html.type !== 'asset') throw new Error('Missing panel HTML');
        let source = String(html.source);
        for (const [name, asset] of Object.entries(bundle)) {
          if (name === 'index.html') continue;
          if (asset.type === 'chunk') {
            source = source.replace(
              /<script\b[^>]*src="[^"]+"[^>]*><\/script>/,
              () =>
                '<script type="module">' +
                asset.code.replace(/<\/script/gi, '<\\/script') +
                '</script>',
            );
          } else if (name.endsWith('.css')) {
            source = source.replace(
              /<link\b[^>]*rel="stylesheet"[^>]*>/,
              () =>
                '<style>' + String(asset.source).replace(/<\/style/gi, '<\\/style') + '</style>',
            );
          } else throw new Error('Unexpected external panel asset: ' + name);
          delete bundle[name];
        }
        source = source.replace(/<link\b[^>]*rel="modulepreload"[^>]*>/g, '');
        if (new TextEncoder().encode(source).length > 2 * 1024 * 1024)
          throw new Error('Panel exceeds MaiBot HTML budget');
        html.source = source;
      },
    },
  };
}
export default defineConfig({
  plugins: [react(), tailwindcss(), singleFile()],
  base: './',
  build: {
    outDir: '../views/maibot',
    emptyOutDir: true,
    target: 'es2022',
    cssCodeSplit: false,
    rolldownOptions: { output: { codeSplitting: false } },
  },
});
