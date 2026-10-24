import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';

const DURATION_MS = 5000;

interface ToastApi {
  show: (text: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [text, setText] = useState('');
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);

  const show = useCallback((next: string) => {
    clearTimeout(timer.current);
    setText(next);
    timer.current = setTimeout(() => {
      setText('');
    }, DURATION_MS);
  }, []);

  useEffect(
    () => () => {
      clearTimeout(timer.current);
    },
    [],
  );

  return (
    <ToastContext.Provider value={{ show }}>
      {children}
      <div
        role="status"
        className="pointer-events-none fixed inset-x-0 bottom-4 flex justify-center px-4"
      >
        {text && <p className="rounded-lg bg-ink px-4 py-2 text-sm text-page shadow-lg">{text}</p>}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const api = useContext(ToastContext);
  if (!api) throw new Error('useToast must be used inside <ToastProvider>');
  return api;
}
