import { retrieveImage } from "@/api/catalog";

export async function GET(_request: Request, { params }: { params: Promise<{ id: string; variant: string }> }) {
  const { id, variant } = await params;
  return retrieveImage(id, variant);
}
