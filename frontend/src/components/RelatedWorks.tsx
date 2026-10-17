import { relatedWorks } from "@/api/catalog";
import { Masonry } from "@/components/Masonry";

export async function RelatedWorks({ id }: { id: string }) {
  try {
    const works = await relatedWorks(id);
    return works.length ? <Masonry works={works} /> : <p>No related works are available.</p>;
  } catch {
    return <p role="status">Related works are temporarily unavailable. Reload to try again.</p>;
  }
}

