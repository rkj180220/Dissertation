import { useCallback, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface Props {
  document: string;
}

export function RfpDocument({ document: rfpDoc }: Props) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    await navigator.clipboard.writeText(rfpDoc);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [rfpDoc]);

  const handleDownload = useCallback(() => {
    const blob = new Blob([rfpDoc], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = window.document.createElement("a");
    a.href = url;
    a.download = "rfp-document.md";
    a.click();
    URL.revokeObjectURL(url);
  }, [rfpDoc]);

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle className="text-lg">RFP Document</CardTitle>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={handleCopy}>
              {copied ? "Copied!" : "Copy Markdown"}
            </Button>
            <Button variant="outline" size="sm" onClick={handleDownload}>
              Download .md
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <div className="prose prose-sm max-w-none dark:prose-invert">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {rfpDoc}
          </ReactMarkdown>
        </div>
      </CardContent>
    </Card>
  );
}
