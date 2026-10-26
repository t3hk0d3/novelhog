import Markdown, { type Components, type UrlTransform } from 'react-markdown';

/** The only image sources a chapter may carry: the inline images the server embeds (spec 8.2). */
const IMAGE_DATA_URI = /^data:image\/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/]*={0,2}$/;

// react-markdown's default filter drops `data:` URLs; chapters embed their images that way, so
// only those are let through and everything else (links, other data types) is removed.
const urlTransform: UrlTransform = (url) => (IMAGE_DATA_URI.test(url) ? url : null);

const components: Components = {
  p: ({ children }) => <p className="mb-[1em]">{children}</p>,
  ul: ({ children }) => <ul className="mb-[1em] list-disc pl-6">{children}</ul>,
  ol: ({ children }) => <ol className="mb-[1em] list-decimal pl-6">{children}</ol>,
  img: ({ src, alt }) =>
    typeof src === 'string' ? (
      <img src={src} alt={alt ?? ''} decoding="async" className="mx-auto my-4 h-auto max-w-full" />
    ) : (
      <span className="text-ink-muted">{alt}</span>
    ),
};

/** A chapter's Markdown as styled elements; raw HTML is never rendered. */
export function ChapterMarkdown({ markdown }: { markdown: string }) {
  return (
    <Markdown urlTransform={urlTransform} components={components}>
      {markdown}
    </Markdown>
  );
}
