import { KetcherEditor } from "./components/KetcherEditor";
import { Layout } from "./components/Layout";
import { QueryPanel } from "./components/QueryPanel";
import { ResultsDisplay } from "./components/ResultsDisplay";
import { useKetcher } from "./hooks/useKetcher";
import { useQuery } from "./hooks/useQuery";

export default function App() {
  const { smiles, setSmiles, iframeRef, setIsReady } = useKetcher("*CC*");
  const { request, setRequest, isLoading, error, data, submit } = useQuery();

  return (
    <Layout>
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_360px]">
        <div className="space-y-6">
          <KetcherEditor
            smiles={smiles}
            iframeRef={iframeRef}
            onReadyChange={setIsReady}
            onChange={(value) => {
              setSmiles(value);
              setRequest({ ...request, smiles: value });
            }}
          />
          <ResultsDisplay data={data} error={error} />
        </div>
        <div>
          <QueryPanel
            request={{ ...request, smiles }}
            onChange={setRequest}
            onSubmit={() => submit({ ...request, smiles })}
            disabled={isLoading || smiles.trim().length === 0}
          />
        </div>
      </div>
    </Layout>
  );
}
