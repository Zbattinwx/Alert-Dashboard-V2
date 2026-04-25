export interface SocialStatus {
  facebook: {
    enabled: boolean;
    configured: boolean;
    page_id: string;
  };
  bluesky: {
    enabled: boolean;
    configured: boolean;
    handle: string;
  };
}

export interface PostResult {
  facebook?: {
    success: boolean;
    post_id?: string;
    error?: string;
  };
  bluesky?: {
    success: boolean;
    uri?: string;
    error?: string;
  };
}

export interface PostHistoryItem {
  id: string;
  platforms: string[];
  message: string;
  has_image: boolean;
  image_count: number;
  results: PostResult;
  success: boolean;
  timestamp: string;
}

export interface SocialTemplates {
  alert_templates: string[];
  lsr_templates: string[];
}
