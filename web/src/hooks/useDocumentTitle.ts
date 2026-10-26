import { useEffect } from 'react';

const APP_NAME = 'novelhog';

/**
 * The browser tab's title while the page is shown: "<name> · novelhog". The title from before is
 * put back when the page goes away or has no name (yet). Pass null while the name is not known.
 */
export function useDocumentTitle(name: string | null): void {
  useEffect(() => {
    if (name === null) return;
    const before = document.title;
    document.title = `${name} · ${APP_NAME}`;
    return () => {
      document.title = before;
    };
  }, [name]);
}
