import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { Route, Routes } from 'react-router';
import { AddNovelDialog } from './components/AddNovelDialog';
import { Header } from './components/Header';
import { PlusIcon } from './components/icons';
import { NotificationsBell } from './components/NotificationsBell';
import { PushBanner } from './components/PushBanner';
import { ToastProvider } from './components/Toast';
import { useLiveEvents } from './hooks/useLiveEvents';
import { usePush } from './hooks/usePush';
import { Library } from './routes/Library';
import { Novel } from './routes/Novel';
import { Reader } from './routes/Reader';

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
    // The shell fills the screen and main takes what the header leaves, so a short page (the
    // reader) can put its bottom bar on the bottom edge instead of leaving blank page below it.
    <div className="flex min-h-dvh flex-col">
      <Header
        addNovel={
          <button
            type="button"
            onClick={openAdd}
            aria-label="Add novel"
            title="Add novel"
            className="group inline-flex size-9 items-center justify-center rounded-full text-ink transition-colors duration-200 hover:bg-line hover:text-accent"
          >
            <span className="flex items-center justify-center transition duration-200 group-hover:scale-105 group-hover:text-accent">
              <PlusIcon />
            </span>
          </button>
        }
        bell={<NotificationsBell push={push} />}
      />
      <PushBanner push={push} />
      <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col p-4">
        <Routes>
          <Route path="/" element={<Library onAddNovel={openAdd} />} />
          <Route path="/novels/:id" element={<Novel />} />
          <Route path="/novels/:id/chapters/:chapterId" element={<Reader />} />
        </Routes>
      </main>
      {adding && (
        <AddNovelDialog
          onClose={() => {
            setAdding(false);
          }}
        />
      )}
    </div>
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
