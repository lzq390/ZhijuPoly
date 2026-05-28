import {
  type CSSProperties,
  type FocusEvent,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
  useEffect,
  useRef,
  useState
} from "react";
import {
  ArrowLeft,
  ArrowRight,
  Atom,
  BarChart3,
  BookOpen,
  ClipboardList,
  Database,
  Bell,
  Menu,
  Microscope,
  Search,
  Settings,
  Sparkles
} from "lucide-react";
import { ConditionalGenerationPage } from "./components/ConditionalGenerationPage";
import { DatabaseAnalysis, type DatasetKey } from "./components/DatabaseAnalysis";
import { DatabaseQueryPage } from "./components/DatabaseQueryPage";
import { KetcherEditor } from "./components/KetcherEditor";
import { KnowledgeSearch } from "./components/KnowledgeSearch";
import { LabDataPage, type LabDataView } from "./components/LabDataPage";
import { Layout } from "./components/Layout";
import { QueryPanel } from "./components/QueryPanel";
import { ResultsDisplay } from "./components/ResultsDisplay";
import { ReverseDesignPage } from "./components/ReverseDesignPage";
import { StructurePreview3D } from "./components/StructurePreview3D";
import { Badge } from "./components/ui/badge";
import { Button } from "./components/ui/button";
import { useKetcher } from "./hooks/useKetcher";
import { usePredict } from "./hooks/usePredict";
import { useQuery } from "./hooks/useQuery";
import {
  type KnowledgeNavigationRequest,
  type PredictableProperty,
  type ResultsTab,
  type WorkspaceMode
} from "./types";

type ActiveModule =
  | "home"
  | "explorer"
  | "reverseDesign"
  | "conditionalGeneration"
  | "databaseQuery"
  | "database"
  | "knowledge"
  | "labData";

type AppRoute = {
  module: ActiveModule;
  datasetKey: DatasetKey | null;
  labDataView?: LabDataView;
};

type KnowledgeNavigationInput = string | KnowledgeNavigationRequest;

const datasetPathByKey: Record<DatasetKey, string> = {
  process: "/database/process",
  property: "/database/property",
  structureEffect: "/database/structure-effect",
  dft: "/database/dft",
  formulation: "/database/formulation"
};

const datasetKeyByPath = Object.fromEntries(
  Object.entries(datasetPathByKey).map(([key, path]) => [path, key as DatasetKey])
) as Record<string, DatasetKey>;

function normalizePath(pathname: string) {
  const normalized = pathname.replace(/\/+$/, "");
  return normalized.length > 0 ? normalized : "/";
}

function routeFromPath(pathname: string): AppRoute {
  const path = normalizePath(pathname);

  if (path === "/explorer") {
    return { module: "explorer", datasetKey: null };
  }

  if (path === "/reverse-design") {
    return { module: "reverseDesign", datasetKey: null };
  }

  if (path === "/conditional-generation") {
    return { module: "conditionalGeneration", datasetKey: null };
  }

  if (path === "/database-query") {
    return { module: "databaseQuery", datasetKey: null };
  }

  if (path === "/knowledge") {
    return { module: "knowledge", datasetKey: null };
  }

  if (path === "/lab-data" || path === "/lab-data/collect") {
    return { module: "labData", datasetKey: null, labDataView: "collect" };
  }

  if (path === "/lab-data/dashboard") {
    return { module: "labData", datasetKey: null, labDataView: "dashboard" };
  }

  if (path === "/database") {
    return { module: "database", datasetKey: null };
  }

  const datasetKey = datasetKeyByPath[path];
  if (datasetKey) {
    return { module: "database", datasetKey };
  }

  return { module: "home", datasetKey: null };
}

function pathFromRoute(route: AppRoute) {
  if (route.module === "explorer") {
    return "/explorer";
  }

  if (route.module === "reverseDesign") {
    return "/reverse-design";
  }

  if (route.module === "conditionalGeneration") {
    return "/conditional-generation";
  }

  if (route.module === "databaseQuery") {
    return "/database-query";
  }

  if (route.module === "knowledge") {
    return "/knowledge";
  }

  if (route.module === "labData") {
    return route.labDataView === "dashboard" ? "/lab-data/dashboard" : "/lab-data/collect";
  }

  if (route.module === "database") {
    return route.datasetKey ? datasetPathByKey[route.datasetKey] : "/database";
  }

  return "/";
}

function getInitialRoute() {
  if (typeof window === "undefined") {
    return { module: "home", datasetKey: null } satisfies AppRoute;
  }

  return routeFromPath(window.location.pathname);
}

function normalizeKnowledgeTerms(terms: string[] | undefined) {
  const normalized: string[] = [];
  const seen = new Set<string>();

  for (const term of terms ?? []) {
    const value = term.trim();
    if (!value) {
      continue;
    }

    const key = value.toLocaleLowerCase();
    if (seen.has(key)) {
      continue;
    }

    seen.add(key);
    normalized.push(value);
  }

  return normalized;
}

function getKnowledgeTermsFromSearch(search: string) {
  return normalizeKnowledgeTerms(new URLSearchParams(search).getAll("term"));
}

type HomeModuleAction = {
  icon: ReactNode;
  label: string;
  onClick?: () => void;
  disabled?: boolean;
};

type HomeCategory = {
  id: string;
  icon: ReactNode;
  title: string;
  description: string;
  variant: "light" | "dark" | "warm";
  backgroundImage?: string;
  actions: HomeModuleAction[];
};

type HomeCardTone = "data" | "light" | "design" | "dark" | "warm";

type HomeCardStyle = CSSProperties & {
  "--spotlight-color"?: string;
  "--spotlight-x"?: string;
  "--spotlight-y"?: string;
};

function HomePage({
  onOpenLabData,
  onOpenExplorer,
  onOpenReverseDesign,
  onOpenConditionalGeneration,
  onOpenDatabaseQuery,
  onOpenDatabase,
  onOpenKnowledge
}: {
  onOpenLabData: () => void;
  onOpenExplorer: () => void;
  onOpenReverseDesign: () => void;
  onOpenConditionalGeneration: () => void;
  onOpenDatabaseQuery: () => void;
  onOpenDatabase: () => void;
  onOpenKnowledge: () => void;
}) {
  const categories: HomeCategory[] = [
    {
      id: "data-and-knowledge",
      icon: <Database className="h-5 w-5" />,
      title: "Data & Knowledge Center",
      description: "Collect, structure, analyze, and retrieve polymer research data from a unified knowledge workspace.",
      variant: "light",
      backgroundImage: "/images/data-knowledge-card-bg.png",
      actions: [
        {
          icon: <ClipboardList className="h-5 w-5" />,
          label: "Lab Data Collection",
          onClick: onOpenLabData
        },
        {
          icon: <Search className="h-5 w-5" />,
          label: "Database Query",
          onClick: onOpenDatabaseQuery
        },
        {
          icon: <BarChart3 className="h-5 w-5" />,
          label: "Database Analytics",
          onClick: onOpenDatabase
        },
        {
          icon: <BookOpen className="h-5 w-5" />,
          label: "Knowledge Search",
          onClick: onOpenKnowledge
        }
      ]
    },
    {
      id: "property-exploration",
      icon: <Atom className="h-5 w-5" />,
      title: "Property Exploration",
      description: "Inspect polymer structures, compare property signals, and move from molecular input to interpretable results.",
      variant: "dark",
      backgroundImage: "/images/property-exploration-card-bg.png",
      actions: [
        {
          icon: <Atom className="h-5 w-5" />,
          label: "Polymer Property Explorer",
          onClick: onOpenExplorer
        },
        {
          icon: <Sparkles className="h-5 w-5" />,
          label: "Prediction Bench",
          disabled: true
        },
        {
          icon: <BarChart3 className="h-5 w-5" />,
          label: "Similarity Atlas",
          disabled: true
        },
        {
          icon: <Microscope className="h-5 w-5" />,
          label: "3D Review Queue",
          disabled: true
        }
      ]
    },
    {
      id: "polymer-design",
      icon: <Sparkles className="h-5 w-5" />,
      title: "Polymer Design",
      description: "Generate and refine candidate polymers from target properties, constraints, and design intent.",
      variant: "warm",
      backgroundImage: "/images/polymer-design-card-bg.png",
      actions: [
        {
          icon: <Sparkles className="h-5 w-5" />,
          label: "Tg Reverse Design",
          onClick: onOpenReverseDesign
        },
        {
          icon: <Microscope className="h-5 w-5" />,
          label: "Conditional Polymer Generation",
          onClick: onOpenConditionalGeneration
        },
        {
          icon: <Database className="h-5 w-5" />,
          label: "Candidate Library",
          disabled: true
        },
        {
          icon: <BookOpen className="h-5 w-5" />,
          label: "Design Review",
          disabled: true
        }
      ]
    }
  ];

  return (
    <section className="home-stage relative -mx-4 -my-5 min-h-screen overflow-hidden md:-mx-8 md:-my-8">
      <div className="relative z-10 flex min-h-screen flex-col">
        <header className="absolute inset-x-0 top-0 z-30 grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 px-5 pt-5 md:px-8 lg:px-10">
          <button
            type="button"
            className="home-top-control inline-flex h-11 items-center gap-2 rounded-full border border-white/20 bg-transparent px-3 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-300 sm:px-4"
            aria-label="Open main menu"
          >
            <Menu className="h-4 w-4" />
            <span className="hidden sm:inline">Menu</span>
          </button>

          <div className="home-brand-mark pointer-events-none flex w-[9.25rem] min-w-0 flex-col items-center justify-self-center text-center sm:w-[10.75rem] md:absolute md:left-1/2 md:top-1/2 md:w-[11.25rem] md:-translate-x-1/2 md:-translate-y-1/2">
            <div className="home-brand-word font-heading w-full text-[clamp(1rem,6vw,1.55rem)] font-semibold uppercase leading-none text-white sm:text-[clamp(1.08rem,3vw,1.8rem)]">
              <span>N</span>
              <span>E</span>
              <span>X</span>
              <span>P</span>
              <span>O</span>
              <span>L</span>
              <span>Y</span>
            </div>
            <div className="home-brand-subtitle mt-1.5 w-full text-center text-[0.68rem] font-medium text-white sm:text-[0.78rem] md:text-[0.84rem]">
              <span>智</span>
              <span>聚</span>
              <span>万</span>
              <span>物</span>
            </div>
          </div>

          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              className="home-icon-control hidden h-10 w-10 items-center justify-center rounded-md bg-transparent text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-300 sm:inline-flex"
              aria-label="Notifications"
            >
              <Bell className="h-4 w-4" />
            </button>
            <button
              type="button"
              className="home-icon-control inline-flex h-10 w-10 items-center justify-center rounded-md bg-transparent text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-300"
              aria-label="Settings"
            >
              <Settings className="h-4 w-4" />
            </button>
            <button
              type="button"
              className="home-top-control hidden h-11 items-center rounded-full border border-white/20 bg-transparent px-4 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-300 lg:inline-flex"
            >
              Log In
            </button>
            <label className="home-top-control hidden h-11 w-[172px] items-center gap-2 rounded-full border border-white/20 bg-transparent px-4 text-white focus-within:ring-2 focus-within:ring-amber-300 md:inline-flex">
              <Search className="h-4 w-4 shrink-0" />
              <input
                className="min-w-0 flex-1 bg-transparent font-mono-ui text-xs font-semibold uppercase tracking-[0.08em] text-white outline-none placeholder:text-white/68"
                aria-label="Search platform"
              />
            </label>
          </div>
        </header>

        <section className="grid flex-1 content-center gap-20 px-5 pb-12 pt-24 md:gap-28 md:px-10 md:pb-16 md:pt-28 lg:gap-36 lg:px-16 lg:pb-20 lg:pt-32">
          <div className="mx-auto mt-14 max-w-5xl animate-fade-up text-center md:mt-16">
            <h1 className="font-heading mx-auto max-w-5xl text-[2.65rem] font-semibold leading-[1.02] text-white drop-shadow-[0_18px_40px_rgba(0,0,0,0.42)] md:text-[4.7rem]">
              The Future of Autonomous Polymer Science
            </h1>
          </div>

          <div className="mt-8 grid auto-rows-fr gap-5 md:mt-10 md:gap-6 lg:mt-12 lg:grid-cols-3">
            {categories.map((category) => (
              <HomeCategoryCard key={category.id} category={category} />
            ))}
          </div>
        </section>
      </div>
    </section>
  );
}

function HomeCategoryCard({ category }: { category: HomeCategory }) {
  const cardBoundsRef = useRef<DOMRect | null>(null);
  const pendingSpotlightRef = useRef<{ element: HTMLElement; x: string; y: string } | null>(null);
  const spotlightFrameRef = useRef<number | null>(null);
  const [isPointerOpen, setIsPointerOpen] = useState(false);
  const [isFocusWithin, setIsFocusWithin] = useState(false);
  const isDark = category.variant === "dark";
  const isWarm = category.variant === "warm";
  const isDataCard = category.id === "data-and-knowledge";
  const isLightPhoto = category.id === "property-exploration";
  const isDesignCard = category.id === "polymer-design";
  const isDrawerOpen = isPointerOpen || isFocusWithin;
  const cardBackgroundStyle: CSSProperties | undefined = category.backgroundImage
    ? {
        backgroundImage: isLightPhoto
          ? `linear-gradient(180deg, rgba(246, 251, 255, 0.7) 0%, rgba(221, 234, 247, 0.58) 52%, rgba(188, 208, 230, 0.68) 100%), url("${category.backgroundImage}")`
          : `linear-gradient(180deg, rgba(5, 9, 18, 0.42) 0%, rgba(5, 9, 18, 0.7) 100%), url("${category.backgroundImage}")`,
        backgroundPosition: "center",
        backgroundSize: "cover"
      }
    : undefined;
  const spotlightColor = isLightPhoto
    ? "rgba(255, 255, 255, 0.52)"
    : isDataCard
      ? "rgba(34, 211, 238, 0.28)"
      : isDesignCard
        ? "rgba(251, 191, 36, 0.24)"
        : "rgba(255, 255, 255, 0.22)";
  const cardStyle: HomeCardStyle = {
    "--spotlight-color": spotlightColor,
    "--spotlight-x": "50%",
    "--spotlight-y": "48%"
  };
  const cardClass = isLightPhoto
    ? "border-white/55 bg-white/[0.24] text-slate-950 shadow-[0_24px_70px_rgba(15,23,42,0.18)] hover:bg-white/[0.3]"
    : isDataCard
      ? "border-cyan-200/24 bg-cyan-950/[0.16] text-cyan-50 shadow-[0_24px_70px_rgba(3,169,244,0.14)] hover:bg-cyan-950/[0.24]"
      : isDesignCard
        ? "border-amber-200/34 bg-amber-950/[0.16] text-amber-50 shadow-[0_24px_70px_rgba(245,158,11,0.14)] hover:bg-amber-950/[0.24]"
        : isDark
          ? "border-cyan-200/20 bg-slate-950/[0.72] text-white shadow-[0_24px_70px_rgba(0,0,0,0.22)] hover:bg-slate-950/[0.84]"
          : isWarm
            ? "border-amber-200/30 bg-amber-50/[0.10] text-white shadow-[0_20px_60px_rgba(0,0,0,0.18)] hover:bg-amber-50/[0.16]"
            : "border-white/20 bg-white/[0.10] text-white shadow-[0_20px_60px_rgba(0,0,0,0.18)] hover:bg-white/[0.16]";
  const iconClass = isLightPhoto
    ? "border-sky-500/25 bg-white/62 text-sky-800 shadow-[0_10px_28px_rgba(14,165,233,0.16)]"
    : isDataCard
      ? "border-cyan-200/30 bg-cyan-200/[0.12] text-cyan-100 shadow-[0_10px_28px_rgba(34,211,238,0.16)]"
      : isDesignCard
        ? "border-amber-200/35 bg-amber-200/[0.14] text-amber-100 shadow-[0_10px_28px_rgba(251,191,36,0.16)]"
        : isDark
          ? "border-cyan-200/20 bg-white/[0.08] text-cyan-200"
          : isWarm
            ? "border-amber-200/30 bg-amber-200/10 text-amber-200"
            : "border-white/20 bg-white/[0.08] text-amber-100";
  const topLineClass = isLightPhoto ? "bg-sky-500/24" : isDataCard ? "bg-cyan-200/55" : isDesignCard ? "bg-amber-200/55" : "bg-white/70";
  const decorClass = isLightPhoto ? "border-sky-600/24 text-sky-800" : isDataCard ? "border-cyan-200/28 text-cyan-100" : isDesignCard ? "border-amber-200/30 text-amber-100" : "border-current";
  const headingClass = isLightPhoto
    ? "text-slate-950 drop-shadow-[0_1px_12px_rgba(255,255,255,0.72)]"
    : isDataCard
      ? "text-cyan-50 drop-shadow-[0_10px_24px_rgba(6,182,212,0.18)]"
      : isDesignCard
        ? "text-amber-50 drop-shadow-[0_10px_24px_rgba(245,158,11,0.18)]"
        : "";
  const descriptionClass = isLightPhoto
    ? "text-slate-800 drop-shadow-[0_1px_12px_rgba(255,255,255,0.72)]"
    : isDataCard
        ? "text-cyan-50/[0.88] drop-shadow-[0_10px_24px_rgba(6,182,212,0.16)]"
      : isDesignCard
        ? "text-amber-50/[0.86] drop-shadow-[0_10px_24px_rgba(245,158,11,0.16)]"
        : "text-white/82 drop-shadow-[0_10px_24px_rgba(0,0,0,0.34)]";
  const buttonTone: HomeCardTone = isLightPhoto ? "light" : isDataCard ? "data" : isDesignCard ? "design" : isDark ? "dark" : "warm";
  const descriptionStateClass = isDrawerOpen ? "-translate-y-[calc(50%_+_2rem)] opacity-0" : "-translate-y-1/2 opacity-100";
  const drawerStateClass = isDrawerOpen
    ? "pointer-events-auto translate-y-0 opacity-100"
    : "pointer-events-none translate-y-[calc(100%+1.25rem)] opacity-0";

  useEffect(() => {
    return () => {
      if (spotlightFrameRef.current !== null) {
        window.cancelAnimationFrame(spotlightFrameRef.current);
      }
    };
  }, []);

  function scheduleSpotlightUpdate(element: HTMLElement, x: string, y: string) {
    pendingSpotlightRef.current = { element, x, y };

    if (spotlightFrameRef.current !== null) {
      return;
    }

    spotlightFrameRef.current = window.requestAnimationFrame(() => {
      const pending = pendingSpotlightRef.current;
      if (pending) {
        pending.element.style.setProperty("--spotlight-x", pending.x);
        pending.element.style.setProperty("--spotlight-y", pending.y);
      }
      spotlightFrameRef.current = null;
    });
  }

  function handleCardMouseEnter(event: MouseEvent<HTMLElement>) {
    setIsPointerOpen(true);
    cardBoundsRef.current = event.currentTarget.getBoundingClientRect();
  }

  function handleCardMouseMove(event: MouseEvent<HTMLElement>) {
    const bounds = cardBoundsRef.current ?? event.currentTarget.getBoundingClientRect();
    const x = ((event.clientX - bounds.left) / bounds.width) * 100;
    const y = ((event.clientY - bounds.top) / bounds.height) * 100;

    scheduleSpotlightUpdate(event.currentTarget, `${x.toFixed(1)}%`, `${y.toFixed(1)}%`);
  }

  function handleCardMouseLeave(event: MouseEvent<HTMLElement>) {
    setIsPointerOpen(false);
    cardBoundsRef.current = null;
    pendingSpotlightRef.current = null;
    if (spotlightFrameRef.current !== null) {
      window.cancelAnimationFrame(spotlightFrameRef.current);
      spotlightFrameRef.current = null;
    }
    event.currentTarget.style.setProperty("--spotlight-x", "50%");
    event.currentTarget.style.setProperty("--spotlight-y", "48%");
  }

  function handleCardFocus() {
    setIsFocusWithin(true);
  }

  function handleCardBlur(event: FocusEvent<HTMLElement>) {
    const nextTarget = event.relatedTarget;

    if (nextTarget instanceof Node && event.currentTarget.contains(nextTarget)) {
      return;
    }

    setIsFocusWithin(false);
  }

  function handleCardKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key !== "Escape") {
      return;
    }

    setIsPointerOpen(false);
    setIsFocusWithin(false);

    if (event.target instanceof HTMLElement) {
      event.target.blur();
    }
  }

  return (
    <section
      id={category.id}
      style={cardStyle}
      tabIndex={0}
      role="group"
      aria-label={`${category.title} module shortcuts`}
      data-drawer-open={isDrawerOpen ? "true" : "false"}
      onMouseEnter={handleCardMouseEnter}
      onMouseMove={handleCardMouseMove}
      onMouseLeave={handleCardMouseLeave}
      onFocus={handleCardFocus}
      onBlur={handleCardBlur}
      onKeyDown={handleCardKeyDown}
      className={[
        "home-card relative flex min-h-[368px] scroll-mt-8 flex-col overflow-hidden rounded-[26px] border p-5 backdrop-blur-[3px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-200/70 md:p-6",
        cardClass
      ].join(" ")}
    >
      {cardBackgroundStyle ? <div className="home-card-bg pointer-events-none absolute inset-0 z-0" style={cardBackgroundStyle} /> : null}
      <div className="home-card-spotlight pointer-events-none absolute inset-0 z-[1]" />
      <div className={["pointer-events-none absolute inset-x-0 top-0 h-px", topLineClass].join(" ")} />
      <div className={["pointer-events-none absolute -right-20 top-10 h-56 w-56 rounded-full border opacity-[0.1]", decorClass].join(" ")} />
      <div className={["pointer-events-none absolute bottom-28 right-10 h-px w-40 rotate-[-22deg] bg-current opacity-[0.12]", decorClass].join(" ")} />

      <div className="relative z-10 flex min-w-0 items-center gap-3">
        <div className={["flex h-12 w-12 items-center justify-center rounded-2xl border", iconClass].join(" ")}>
          {category.icon}
        </div>
        <h2
          className={[
            "font-heading min-w-0 truncate text-[1.62rem] font-semibold leading-none md:text-[1.82rem]",
            headingClass
          ].join(" ")}
          title={category.title}
        >
          {category.title}
        </h2>
      </div>

      <div
        className={[
          "pointer-events-none absolute inset-x-5 top-1/2 z-10 max-w-[22rem] transition-all duration-500 ease-out md:inset-x-6",
          descriptionStateClass
        ].join(" ")}
      >
        <p
          className={[
            "text-[1.08rem] font-medium leading-7",
            descriptionClass
          ].join(" ")}
        >
          {category.description}
        </p>
      </div>

      <div
        className={[
          "absolute inset-x-5 bottom-5 z-20 transition-all duration-500 ease-out md:inset-x-6 md:bottom-6",
          drawerStateClass
        ].join(" ")}
        aria-hidden={!isDrawerOpen}
      >
        <div className="grid gap-2.5">
          {category.actions.map((action) => (
            <HomeActionButton key={action.label} action={action} drawerOpen={isDrawerOpen} tone={buttonTone} />
          ))}
        </div>
      </div>
    </section>
  );
}

function HomeActionButton({
  action,
  drawerOpen,
  tone
}: {
  action: HomeModuleAction;
  drawerOpen: boolean;
  tone: "data" | "light" | "design" | "dark" | "warm";
}) {
  const buttonClass =
    tone === "light"
      ? "border-slate-900/10 bg-white/[0.72] text-slate-950 shadow-[0_10px_26px_rgba(15,23,42,0.08)] hover:border-sky-500/30 hover:bg-white/[0.88]"
      : tone === "data"
        ? "border-cyan-200/20 bg-cyan-950/[0.34] text-cyan-50 shadow-[0_10px_26px_rgba(6,182,212,0.08)] hover:border-cyan-200/48 hover:bg-cyan-900/[0.44]"
        : tone === "design"
          ? "border-amber-100/24 bg-slate-950/[0.36] text-amber-50 shadow-[0_10px_26px_rgba(2,6,23,0.16)] hover:border-amber-100/55 hover:bg-slate-900/[0.52]"
          : tone === "dark"
            ? "border-white/10 bg-white/[0.07] text-white hover:border-cyan-200/40 hover:bg-white/[0.12]"
            : "border-amber-200/24 bg-black/20 text-white hover:border-amber-200/50 hover:bg-black/30";
  const iconClass =
    tone === "light"
      ? "bg-sky-100/[0.85] text-sky-800"
      : tone === "data"
        ? "bg-cyan-200/[0.14] text-cyan-100"
      : tone === "design"
          ? "bg-amber-100/[0.14] text-amber-100"
          : tone === "dark"
            ? "bg-white/10 text-cyan-200"
            : "bg-amber-200/12 text-amber-200";

  return (
    <button
      type="button"
      onClick={action.onClick}
      disabled={action.disabled}
      tabIndex={drawerOpen && !action.disabled ? 0 : -1}
      className={[
        "home-action-button grid min-h-[50px] grid-cols-[2rem_minmax(0,1fr)_1.25rem] items-center gap-2 rounded-[15px] border px-2.5 text-left text-[0.95rem] font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default disabled:opacity-55",
        buttonClass
      ].join(" ")}
    >
      <span className={["flex h-8 w-8 items-center justify-center rounded-xl [&_svg]:h-4 [&_svg]:w-4", iconClass].join(" ")}>
        {action.icon}
      </span>
      <span className="min-w-0 truncate">{action.label}</span>
      <ArrowRight className="home-action-arrow h-4 w-4 justify-self-end" />
    </button>
  );
}

export default function App() {
  const [activeModule, setActiveModule] = useState<ActiveModule>(() => getInitialRoute().module);
  const [selectedDatasetKey, setSelectedDatasetKey] = useState<DatasetKey | null>(() => getInitialRoute().datasetKey);
  const [labDataView, setLabDataView] = useState<LabDataView>(() => getInitialRoute().labDataView ?? "collect");
  const [knowledgeInitialQuery, setKnowledgeInitialQuery] = useState(() => {
    if (typeof window === "undefined") {
      return "";
    }
    return new URLSearchParams(window.location.search).get("q") ?? "";
  });
  const [knowledgeInitialTerms, setKnowledgeInitialTerms] = useState(() => {
    if (typeof window === "undefined") {
      return [] as string[];
    }
    return getKnowledgeTermsFromSearch(window.location.search);
  });
  const [hasOpenedExplorer, setHasOpenedExplorer] = useState(() => getInitialRoute().module === "explorer");
  const [preserveReverseDesignForKnowledge, setPreserveReverseDesignForKnowledge] = useState(false);
  const { smiles, setSmiles, iframeRef, setIsReady } = useKetcher("*CC*");
  const { request, setRequest, isLoading, error, data, submit } = useQuery();
  const predict = usePredict();
  const [panelMode, setPanelMode] = useState<WorkspaceMode>("query");
  const [activeResultsTab, setActiveResultsTab] = useState<ResultsTab>("query");
  const [selectedProperties, setSelectedProperties] = useState<PredictableProperty[]>([]);

  const canQuery =
    !isLoading &&
    smiles.trim().length > 0 &&
    (request.match_mode === "structure" || request.property_name !== null);
  const canPredict = !predict.isLoading && smiles.trim().length > 0 && selectedProperties.length > 0;

  const activeMode =
    panelMode === "predict"
      ? "Property prediction"
      : request.match_mode === "property"
        ? "Property similarity"
        : "Structural similarity";
  const activeModeLabel =
    panelMode === "predict"
      ? "Property Prediction"
      : request.match_mode === "property"
        ? "Property Similarity"
        : "Structural Similarity";

  const resultCount =
    activeResultsTab === "predict" ? Object.keys(predict.data?.predictions ?? {}).length : data?.total ?? 0;
  const resultTiming =
    activeResultsTab === "predict" ? predict.data?.query_time_ms : data?.query_time_ms;

  async function handleQuerySubmit() {
    setActiveResultsTab("query");
    await submit({ ...request, smiles });
  }

  async function handlePredictSubmit() {
    setActiveResultsTab("predict");
    try {
      await predict.submit({
        smiles,
        properties: selectedProperties
      });
    } catch {
      // Error state is already captured by the hook and shown in the results panel.
    }
  }

  const resultPanelTitle = activeResultsTab === "predict" ? "Prediction Results" : "Similarity Matching Results";
  const resultPanelDescription =
    activeResultsTab === "predict"
      ? "After prediction finishes, selected property values and calculation time appear here."
      : "After similarity matching runs, summaries, 2D structures, SMILES, and similarity scores appear here.";
  const resultPrimaryBadge =
    activeResultsTab === "predict"
      ? predict.data
        ? `${Object.keys(predict.data.predictions).length} predictions`
        : "No predictions"
      : data
        ? `${data.total} records`
        : "No results";
  const resultSecondaryBadge =
    activeResultsTab === "predict"
      ? predict.isLoading
        ? "Predicting"
        : "Prediction mode"
      : request.match_mode === "property"
        ? "Property similarity"
        : "Structural similarity";

  useEffect(() => {
    if (typeof window === "undefined" || !("scrollRestoration" in window.history)) {
      return;
    }

    const previousScrollRestoration = window.history.scrollRestoration;
    window.history.scrollRestoration = "manual";

    return () => {
      window.history.scrollRestoration = previousScrollRestoration;
    };
  }, []);

  function applyRoute(route: AppRoute) {
    setActiveModule(route.module);
    setSelectedDatasetKey(route.module === "database" ? route.datasetKey : null);
    setLabDataView(route.module === "labData" ? route.labDataView ?? "collect" : "collect");

    if (route.module === "explorer") {
      setHasOpenedExplorer(true);
    }
    if (route.module !== "knowledge") {
      setPreserveReverseDesignForKnowledge(false);
    }
  }

  function navigate(route: AppRoute) {
    const path = pathFromRoute(route);

    if (typeof window !== "undefined") {
      if (normalizePath(window.location.pathname) !== path) {
        window.history.pushState(route, "", path);
      }
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    }

    applyRoute(route);
  }

  useEffect(() => {
    function handlePopState() {
      const route = routeFromPath(window.location.pathname);
      if (route.module === "knowledge") {
        setKnowledgeInitialQuery(new URLSearchParams(window.location.search).get("q") ?? "");
        setKnowledgeInitialTerms(getKnowledgeTermsFromSearch(window.location.search));
      }
      applyRoute(route);
    }

    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  function openExplorer() {
    navigate({ module: "explorer", datasetKey: null });
  }

  function openReverseDesign() {
    navigate({ module: "reverseDesign", datasetKey: null });
  }

  function openConditionalGeneration() {
    navigate({ module: "conditionalGeneration", datasetKey: null });
  }

  function openDatabaseQuery() {
    navigate({ module: "databaseQuery", datasetKey: null });
  }

  function openDatabase() {
    navigate({ module: "database", datasetKey: null });
  }

  function openKnowledge(input?: KnowledgeNavigationInput) {
    const rawQuery = typeof input === "string" ? input : input?.query;
    const trimmedQuery = rawQuery?.trim() ?? "";
    const terms = typeof input === "string" ? [] : normalizeKnowledgeTerms(input?.terms);
    const route = { module: "knowledge", datasetKey: null } satisfies AppRoute;
    const searchParams = new URLSearchParams();

    if (trimmedQuery) {
      searchParams.set("q", trimmedQuery);
    }
    for (const term of terms) {
      searchParams.append("term", term);
    }

    const queryString = searchParams.toString();
    const path = queryString ? `/knowledge?${queryString}` : "/knowledge";

    setPreserveReverseDesignForKnowledge(activeModule === "reverseDesign");
    setKnowledgeInitialQuery(trimmedQuery);
    setKnowledgeInitialTerms(terms);

    if (typeof window !== "undefined") {
      if (`${normalizePath(window.location.pathname)}${window.location.search}` !== path) {
        window.history.pushState(route, "", path);
      }
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    }

    applyRoute(route);
  }

  function openLabData(view: LabDataView = "collect") {
    navigate({ module: "labData", datasetKey: null, labDataView: view });
  }

  return (
    <Layout fullBleed={activeModule === "home"}>
      <div className={activeModule === "home" ? "contents" : "hidden"}>
        <HomePage
          onOpenLabData={() => openLabData("collect")}
          onOpenExplorer={openExplorer}
          onOpenReverseDesign={openReverseDesign}
          onOpenConditionalGeneration={openConditionalGeneration}
          onOpenDatabaseQuery={openDatabaseQuery}
          onOpenDatabase={openDatabase}
          onOpenKnowledge={openKnowledge}
        />
      </div>

      {activeModule === "databaseQuery" ? (
        <DatabaseQueryPage onBackHome={() => navigate({ module: "home", datasetKey: null })} />
      ) : null}

      {activeModule === "database" ? (
        <DatabaseAnalysis
          selectedKey={selectedDatasetKey}
          onBackHome={() => navigate({ module: "home", datasetKey: null })}
          onBackDatabase={() => navigate({ module: "database", datasetKey: null })}
          onOpenDataset={(datasetKey) => navigate({ module: "database", datasetKey })}
        />
      ) : null}

      {activeModule === "knowledge" ? (
        <KnowledgeSearch
          onBackHome={() => navigate({ module: "home", datasetKey: null })}
          initialQuery={knowledgeInitialQuery}
          initialTerms={knowledgeInitialTerms}
        />
      ) : null}

      {activeModule === "labData" ? (
        <LabDataPage
          view={labDataView}
          onBackHome={() => navigate({ module: "home", datasetKey: null })}
          onChangeView={(view) => openLabData(view)}
        />
      ) : null}

      {activeModule === "conditionalGeneration" ? (
        <ConditionalGenerationPage onBackHome={() => navigate({ module: "home", datasetKey: null })} />
      ) : null}

      {activeModule === "reverseDesign" || preserveReverseDesignForKnowledge ? (
        <div
          className={activeModule === "reverseDesign" ? "contents" : "hidden"}
          aria-hidden={activeModule !== "reverseDesign"}
        >
          <ReverseDesignPage
            onBackHome={() => navigate({ module: "home", datasetKey: null })}
            onOpenKnowledge={openKnowledge}
          />
        </div>
      ) : null}

      {hasOpenedExplorer ? (
        <div className={activeModule === "explorer" ? "contents" : "hidden"} aria-hidden={activeModule !== "explorer"}>
          <nav className="flex flex-col gap-3 rounded-[26px] border border-white/70 bg-white/80 px-4 py-4 shadow-sm backdrop-blur md:flex-row md:items-center md:justify-between md:px-5">
            <div className="flex items-center gap-3">
              <Button type="button" variant="outline" onClick={() => navigate({ module: "home", datasetKey: null })}>
                <ArrowLeft className="mr-2 h-4 w-4" />
                Home
              </Button>
              <div>
                <div className="text-[11px] font-medium uppercase tracking-[0.2em] text-teal-700/70">Current Module</div>
                <div className="font-heading text-lg font-semibold tracking-tight text-slate-950">
                  Polymer Property Explorer
                </div>
              </div>
            </div>
            <Badge className="bg-teal-50 text-teal-800">Explorer</Badge>
          </nav>

          <section className="hero-glow mesh-surface relative overflow-hidden rounded-[36px] border border-white/70 px-6 py-6 md:px-8 md:py-8">
            <div className="pointer-events-none absolute inset-y-0 right-0 hidden w-[36%] bg-[radial-gradient(circle_at_center,rgba(15,118,110,0.14),transparent_58%)] lg:block" />
            <div className="pointer-events-none absolute -right-10 top-12 h-40 w-40 rounded-full border border-white/40 bg-white/20 blur-2xl" />
            <div className="pointer-events-none absolute left-8 top-24 h-24 w-24 rounded-full bg-teal-300/20 blur-3xl" />

            <div className="animate-fade-up">
              <div className="flex flex-wrap items-center gap-3">
                <div className="rounded-full border border-white/80 bg-white/80 px-4 py-2 text-sm font-semibold tracking-[0.16em] text-slate-950 shadow-sm">
                  NEXPOLY
                </div>
                <Badge>Polymer Similarity Matching & Property Prediction</Badge>
              </div>

              <div className="mt-6 overflow-x-auto">
                <h1 className="font-heading whitespace-nowrap text-[2.5rem] font-semibold tracking-[-0.04em] text-slate-950 md:text-[4rem] md:leading-[0.95]">
                  Polymer Property Explorer
                </h1>
                <p className="mt-4 whitespace-nowrap text-base leading-7 text-slate-600 md:text-lg">
                  Bring structure editing, similarity matching, 3D review, and property prediction into one focused research workspace.
                </p>
              </div>

              <div className="mt-8 grid gap-3 md:grid-cols-3">
                <div className="flex min-h-[188px] flex-col justify-center rounded-[26px] border border-white/80 bg-white/80 p-5 text-center shadow-sm backdrop-blur">
                  <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-mutedForeground">
                    {panelMode === "predict" ? <Sparkles className="h-4 w-4 text-teal-600" /> : <Atom className="h-4 w-4 text-teal-600" />}
                    Current Mode
                  </div>
                  <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight text-slate-950">
                    {activeModeLabel}
                  </div>
                  <div className="mt-2 text-sm leading-6 text-mutedForeground">
                    {panelMode === "predict"
                      ? "Select target properties in the control card and run prediction for the current structure."
                      : "Switch between structural similarity and property similarity matching in the control card."}
                  </div>
                </div>

                <div className="flex min-h-[188px] flex-col justify-center rounded-[26px] border border-white/80 bg-white/80 p-5 text-center shadow-sm backdrop-blur">
                  <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-mutedForeground">
                    <Microscope className="h-4 w-4 text-sky-600" />
                    Structure Input
                  </div>
                  <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight text-slate-950">
                    {smiles.trim().length > 0 ? "Ready" : "Waiting"}
                  </div>
                  <div className="mt-2 text-sm leading-6 text-mutedForeground">
                    The structure editor keeps the SMILES input updated for matching or prediction.
                  </div>
                </div>

                <div className="flex min-h-[188px] flex-col justify-center rounded-[26px] border border-white/80 bg-slate-950 p-5 text-center text-slate-50 shadow-[0_22px_50px_rgba(8,17,31,0.2)]">
                  <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-slate-400">
                    <Database className="h-4 w-4 text-teal-300" />
                    Latest Results
                  </div>
                  <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight">{resultCount}</div>
                  <div className="mt-2 text-sm leading-6 text-slate-300">
                    {resultTiming ? `${resultTiming.toFixed(1)} ms returned` : "Result count and latency appear after execution."}
                  </div>
                </div>
              </div>
            </div>
          </section>

          <section>
            <div className="grid items-stretch gap-6 xl:grid-cols-[minmax(0,1.22fr)_minmax(0,0.92fr)]">
              <div className="min-w-0">
                <KetcherEditor
                  smiles={smiles}
                  iframeRef={iframeRef}
                  onReadyChange={setIsReady}
                  onChange={(value) => {
                    setSmiles(value);
                    setRequest({ ...request, smiles: value });
                  }}
                />
              </div>

              <div className="flex min-w-0 flex-col gap-6">
                <StructurePreview3D smiles={smiles} />
                <QueryPanel
                  className="w-full self-start"
                  mode={panelMode}
                  onModeChange={setPanelMode}
                  request={{ ...request, smiles }}
                  onChange={setRequest}
                  onQuerySubmit={handleQuerySubmit}
                  onPredictSubmit={handlePredictSubmit}
                  selectedProperties={selectedProperties}
                  onSelectedPropertiesChange={setSelectedProperties}
                  queryDisabled={!canQuery}
                  predictDisabled={!canPredict}
                  isQueryLoading={isLoading}
                  isPredicting={predict.isLoading}
                />
              </div>
            </div>
          </section>

          <section className="relative pt-2">
            <div className="absolute left-0 right-0 top-0 h-px bg-gradient-to-r from-transparent via-slate-400/40 to-transparent" />
            <div className="pt-6">
              <div className="overflow-hidden rounded-[32px] border border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(243,248,250,0.92)_100%)] shadow-soft">
                <div className="border-b border-slate-200/80 px-6 py-5 md:px-8">
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
                    <div>
                      <div className="text-xs font-medium uppercase tracking-[0.18em] text-teal-700/70">Results</div>
                      <h2 className="font-heading mt-2 text-[1.8rem] font-semibold tracking-tight text-slate-950">
                        {resultPanelTitle}
                      </h2>
                      <p className="mt-1 text-sm leading-6 text-mutedForeground">{resultPanelDescription}</p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Badge className="bg-slate-100 text-slate-700">{resultPrimaryBadge}</Badge>
                      <Badge className="bg-slate-100 text-slate-700">{resultSecondaryBadge}</Badge>
                    </div>
                  </div>
                </div>
                <div className="px-4 py-4 md:px-5 md:py-5">
                  <ResultsDisplay
                    data={data}
                    error={error}
                    isLoading={isLoading}
                    request={{ ...request, smiles }}
                    predictData={predict.data}
                    isPredicting={predict.isLoading}
                    predictError={predict.error}
                    activeTab={activeResultsTab}
                    onTabChange={setActiveResultsTab}
                  />
                </div>
              </div>
            </div>
          </section>
        </div>
      ) : null}
    </Layout>
  );
}
