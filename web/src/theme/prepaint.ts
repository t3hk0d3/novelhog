export const THEME_STORAGE_KEY = 'novelhog.theme';

/**
 * Source of the inline script in index.html (injected by the Vite plugin in vite.config.ts).
 * It applies a stored light/dark choice before first paint; anything else leaves System.
 */
export const prepaintScript = `try{var c=localStorage.getItem('${THEME_STORAGE_KEY}');if(c==='light'||c==='dark'){document.documentElement.setAttribute('data-theme',c);}}catch(e){}`;
