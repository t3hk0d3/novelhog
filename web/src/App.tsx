import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { Route, Routes } from 'react-router';
import { AddNovelDialog } from './components/AddNovelDialog';
import { Header } from './components/Header';
import { NotificationsBell } from './components/NotificationsBell';
import { PushBanner } from './components/PushBanner';
import { ToastProvider } from './components/Toast';
import { useLiveEvents } from './hooks/useLiveEvents';
import { usePush } from './hooks/usePush';
import { Library } from './routes/Library';
import { Novel } from './routes/Novel';

const defaultQueryClient = new QueryClient();

/** Registers the push service worker where the browser allows one (secure contexts only). */
function registerServiceWorker(): void {
  if (!window.isSecureContext || !('serviceWorker' in navigator)) return;
  navigator.serviceWorker.register('/sw.js').catch((error: unknown) => {
    console.warn('Service worker registration failed', error);
  });
}

/** Header, banner and routes; inside the query and toast providers. */
function Shell() {
  const [adding, setAdding] = useState(false);
  const push = usePush();
  const openAdd = () => {
    setAdding(true);
  };
  return (
    <>
      <Header
        addNovel={
          <button
            type="button"
            onClick={openAdd}
            className="rounded-lg bg-accent px-3 py-1.5 text-sm font-semibold text-accent-ink"
          >
            Add novel
          </button>
        }
        bell={<NotificationsBell push={push} />}
      />
      <PushBanner push={push} />
      <main className="mx-auto max-w-5xl p-4">
        <Routes>
          <Route path="/" element={<Library onAddNovel={openAdd} />} />
          <Route path="/novels/:id" element={<Novel />} />
        </Routes>
      </main>
      {adding && (
        <AddNovelDialog
          onClose={() => {
            setAdding(false);
          }}
        />
      )}
    </>
  );
}

/** App shell; main.tsx supplies the router. */
export function App({ queryClient = defaultQueryClient }: { queryClient?: QueryClient }) {
  useLiveEvents(queryClient);
  useEffect(registerServiceWorker, []);
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <Shell />
      </ToastProvider>
    </QueryClientProvider>
  );
}
