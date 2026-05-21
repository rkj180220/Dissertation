import { Navigate, Route, Routes } from "react-router-dom";
import { RootLayout } from "@/components/layout/RootLayout";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import ChatPage from "@/pages/ChatPage";
import ResultsPage from "@/pages/ResultsPage";
import NotFoundPage from "@/pages/NotFoundPage";

export default function App() {
  return (
    <Routes>
      <Route element={<RootLayout />}>
        <Route index element={<Navigate to="/chat" replace />} />
        <Route
          path="chat"
          element={
            <ErrorBoundary title="Chat unavailable">
              <ChatPage />
            </ErrorBoundary>
          }
        />
        <Route
          path="results"
          element={
            <ErrorBoundary title="Results unavailable">
              <ResultsPage />
            </ErrorBoundary>
          }
        />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
