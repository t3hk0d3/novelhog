import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState } from 'react';
import { Route, Routes } from 'react-router';
import { AddNovelDialog } from './components/AddNovelDialog';
import { Header } from './components/Header';
import { ToastProvider } from './components/Toast';
import { useLiveEvents } from './hooks/useLiveEvents';
import { Library } from './routes/Library';
import { Novel } from './routes/Novel';

const defaultQueryClient = new QueryClient();

/** App shell; main.tsx supplies the router. */
export function App({ queryClient = defaultQueryClient }: { queryClient?: QueryClient }) {
  useLiveEvents(queryClient);
  const [adding, setAdding] = useState(false);
  const openAdd = () => {
    setAdding(true);
  };
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
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
        />
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
      </ToastProvider>
    </QueryClientProvider>
  );
}
