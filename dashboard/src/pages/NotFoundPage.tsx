import { Link } from "react-router-dom";

export default function NotFoundPage() {
  return (
    <div className="flex flex-col items-center justify-center gap-4 py-20">
      <h1 className="text-4xl font-bold">404</h1>
      <p className="text-muted-foreground">Page not found</p>
      <Link
        to="/chat"
        className="text-sm underline underline-offset-4 hover:text-primary"
      >
        Go to Chat
      </Link>
    </div>
  );
}
