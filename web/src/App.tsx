import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Route, Routes, useParams } from 'react-router';
import { Header } from './components/Header';
import { ToastProvider } from './components/Toast';

const defaultQueryClient = new QueryClient();

function LibraryPlaceholder() {
  return <p className="text-ink-muted">Library (Task 33)</p>;
}

function NovelPlaceholder() {
  const { id } = useParams();
  return <p className="text-ink-muted">Novel {id} (Task 34)</p>;
}

/** App shell; main.tsx supplies the router. */
export function App({ queryClient = defaultQueryClient }: { queryClient?: QueryClient }) {
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <Header />
        <main className="mx-auto max-w-5xl p-4">
          <Routes>
            <Route path="/" element={<LibraryPlaceholder />} />
            <Route path="/novels/:id" element={<NovelPlaceholder />} />
          </Routes>
        </main>
      </ToastProvider>
    </QueryClientProvider>
  );
}
