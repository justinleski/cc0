export type Work = {
  id: string;
  title: string;
  author: string;
  published: string;
  width: number;
  height: number;
  src: string;
  alt: string;
  tags: string[];
  sourceName: string;
  sourceUrl: string;
};

const met = "The Metropolitan Museum of Art";

/**
 * Local mock catalog. Image files are Open Access reproductions from The Met,
 * which releases those images under CC0. They stand in until the API serves
 * verified CC0 assets.
 */
export const works: Work[] = [
  {
    id: "11417",
    title: "Washington Crossing the Delaware",
    author: "Emanuel Leutze",
    published: "1851",
    width: 599,
    height: 351,
    src: "/works/11417.jpg",
    alt: "Painting of boats crossing a river through ice, with a standing figure in a dark boat.",
    tags: ["landscape"],
    sourceName: met,
    sourceUrl: "https://www.metmuseum.org/art/collection/search/11417",
  },
  {
    id: "436535",
    title: "Wheat Field with Cypresses",
    author: "Vincent van Gogh",
    published: "1889",
    width: 599,
    height: 477,
    src: "/works/436535.jpg",
    alt: "Painting of a yellow-green wheat field, dark cypresses, and a swirling blue sky.",
    tags: ["landscape"],
    sourceName: met,
    sourceUrl: "https://www.metmuseum.org/art/collection/search/436535",
  },
  {
    id: "10154",
    title: "The Rocky Mountains, Lander's Peak",
    author: "Albert Bierstadt",
    published: "1863",
    width: 599,
    height: 359,
    src: "/works/10154.jpg",
    alt: "Wide landscape painting of a lake, a camp, and a sunlit mountain peak.",
    tags: ["landscape"],
    sourceName: met,
    sourceUrl: "https://www.metmuseum.org/art/collection/search/10154",
  },
  {
    id: "16577",
    title: "Winter Scene in Moonlight",
    author: "Henry Farrer",
    published: "1869",
    width: 600,
    height: 472,
    src: "/works/16577.jpg",
    alt: "Moonlit winter landscape with a pale sky over a dark snowy bank.",
    tags: ["landscape"],
    sourceName: met,
    sourceUrl: "https://www.metmuseum.org/art/collection/search/16577",
  },
  {
    id: "437980",
    title: "Cypresses",
    author: "Vincent van Gogh",
    published: "1889",
    width: 495,
    height: 624,
    src: "/works/437980.jpg",
    alt: "Tall dark cypress trees rising against a swirling blue and white sky.",
    tags: ["landscape"],
    sourceName: met,
    sourceUrl: "https://www.metmuseum.org/art/collection/search/437980",
  },
  {
    id: "436524",
    title: "Sunflowers",
    author: "Vincent van Gogh",
    published: "1887",
    width: 599,
    height: 423,
    src: "/works/436524.jpg",
    alt: "Still life of cut sunflowers lying on a yellow surface.",
    tags: ["still-life"],
    sourceName: met,
    sourceUrl: "https://www.metmuseum.org/art/collection/search/436524",
  },
  {
    id: "436533",
    title: "Shoes",
    author: "Vincent van Gogh",
    published: "1888",
    width: 599,
    height: 501,
    src: "/works/436533.jpg",
    alt: "Still life of a worn pair of leather shoes on a pale ground.",
    tags: ["still-life"],
    sourceName: met,
    sourceUrl: "https://www.metmuseum.org/art/collection/search/436533",
  },
  {
    id: "437397",
    title: "Self-Portrait",
    author: "Rembrandt (Rembrandt van Rijn)",
    published: "1660",
    width: 515,
    height: 625,
    src: "/works/437397.jpg",
    alt: "Painted self-portrait of a man in dark clothing against a dark background.",
    tags: ["portrait"],
    sourceName: met,
    sourceUrl: "https://www.metmuseum.org/art/collection/search/437397",
  },
  {
    id: "436121",
    title: "A Woman Seated beside a Vase of Flowers",
    author: "Edgar Degas",
    published: "1865",
    width: 600,
    height: 488,
    src: "/works/436121.jpg",
    alt: "Painting of a seated woman in a light dress beside a large bouquet.",
    tags: ["portrait"],
    sourceName: met,
    sourceUrl: "https://www.metmuseum.org/art/collection/search/436121",
  },
  {
    id: "437654",
    title: "Circus Sideshow (Parade de cirque)",
    author: "Georges Seurat",
    published: "1887–88",
    width: 600,
    height: 401,
    src: "/works/437654.jpg",
    alt: "Night scene of performers on a gaslit circus platform above a crowd.",
    tags: ["figure"],
    sourceName: met,
    sourceUrl: "https://www.metmuseum.org/art/collection/search/437654",
  },
  {
    id: "459136",
    title: "Madonna and Child",
    author: "Simone Martini",
    published: "ca. 1326",
    width: 457,
    height: 624,
    src: "/works/459136.jpg",
    alt: "Tempera panel of a seated figure in a dark robe holding a child.",
    tags: ["figure"],
    sourceName: met,
    sourceUrl: "https://www.metmuseum.org/art/collection/search/459136",
  },
  {
    id: "436105",
    title: "The Death of Socrates",
    author: "Jacques Louis David",
    published: "1787",
    width: 599,
    height: 399,
    src: "/works/436105.jpg",
    alt: "Painting of a group of men in a stone room gathered around a seated figure.",
    tags: ["figure"],
    sourceName: met,
    sourceUrl: "https://www.metmuseum.org/art/collection/search/436105",
  },
  {
    id: "436966",
    title: "The Adoration of the Shepherds",
    author: "Andrea Mantegna",
    published: "shortly after 1450",
    width: 599,
    height: 441,
    src: "/works/436966.jpg",
    alt: "Painting of figures kneeling around an infant in a rocky landscape.",
    tags: ["figure"],
    sourceName: met,
    sourceUrl: "https://www.metmuseum.org/art/collection/search/436966",
  },
  {
    id: "459110",
    title: "Young Girl Bathing",
    author: "Auguste Renoir",
    published: "1892",
    width: 500,
    height: 625,
    src: "/works/459110.jpg",
    alt: "Painting of a girl seated at the edge of water, seen from the side.",
    tags: ["figure"],
    sourceName: met,
    sourceUrl: "https://www.metmuseum.org/art/collection/search/459110",
  },
];

export function getWork(id: string): Work | undefined {
  return works.find((work) => work.id === id);
}

export function filterWorks(query: string): Work[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return works;
  return works.filter((work) => {
    const haystack = [work.title, work.author, work.published, ...work.tags]
      .join(" ")
      .toLowerCase();
    return haystack.includes(needle);
  });
}

export function relatedWorks(work: Work): Work[] {
  return works.filter(
    (other) => other.id !== work.id && other.tags.some((tag) => work.tags.includes(tag)),
  );
}

export type SuggestMode = "cohesive" | "complement";

export function suggestWorks(savedIds: string[], mode: SuggestMode): Work[] {
  const saved = new Set(savedIds);
  const savedWorks = works.filter((work) => saved.has(work.id));
  const counts = new Map<string, number>();
  for (const work of savedWorks) {
    for (const tag of work.tags) {
      counts.set(tag, (counts.get(tag) ?? 0) + 1);
    }
  }
  const dominant = [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0];
  return works.filter((work) => {
    if (saved.has(work.id)) return false;
    const shares = dominant ? work.tags.includes(dominant) : false;
    return mode === "cohesive" ? shares : !shares;
  });
}
